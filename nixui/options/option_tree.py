import dataclasses
import functools
import uuid

from treelib import Tree, Node

from nixui.options.attribute import Attribute
from nixui.options.option_definition import OptionDefinition, Undefined
from nixui.utils.logger import logger


@dataclasses.dataclass
class OptionData:
    description: str = Undefined
    readOnly: bool = Undefined
    _type: str = Undefined
    system_default_definition: OptionDefinition = OptionDefinition.undefined()
    configured_definition: OptionDefinition = OptionDefinition.undefined()
    in_memory_definition: OptionDefinition = OptionDefinition.undefined()

    # based on https://stackoverflow.com/a/61426351
    def update(self, new):
        for key, value in new.items():
            if hasattr(self, key):
                setattr(self, key, value)
            elif hasattr(self, f'_{key}'):
                setattr(self, f'_{key}', value)

    def copy(self):
        return dataclasses.replace(self)


class OptionTree:
    """
    Data structure managing option retrieval, option setting, and special handling for attribute sets of submodules

    system_options: mapping from Attribute to dict containing {'description', 'readOnly', 'type', 'default'}
    config_options: mapping from Attribute to configured definition
    """
    def __init__(self, system_option_data, config_options):
        # load data into Tree with OptionData leaves
        self.tree = Tree()
        self.tree.create_node(identifier=Attribute([]), data=OptionData(_type='attribute set'))

        # cache for faster lookup of changed nodes
        self.in_memory_change_cache = {}
        self.configured_change_cache = {}
        self.change_marker = None

        # insert option data with parent option data inserted first via `sorted`
        sort_key = lambda s: str(s[0]).replace('"<name>"', '')  # todo, clean up this hack
        for option_path, option_data_dict in sorted(system_option_data.items(), key=sort_key):
            self._upsert_node_data(option_path, option_data_dict)
        for option_path, option_definition in config_options.items():
            if self.tree.get_node(option_path):
                self._upsert_node_data(option_path, {'configured_definition': option_definition})
                self.configured_change_cache[option_path] = option_definition
            else:
                logger.error(f'"{option_path}" not a valid option, ignored')

    def __hash__(self):
        return hash(self.change_marker)

    def _update_in_memory_change_cache(self, attribute, option_definition):
        in_memory_definition = self.tree.get_node(attribute).data.in_memory_definition
        if in_memory_definition == self.tree.get_node(attribute).data.configured_definition:
            if attribute in self.in_memory_change_cache:
                del self.in_memory_change_cache[attribute]
        else:
            self.in_memory_change_cache[attribute] = option_definition
        self.change_marker = uuid.uuid4()

    def _upsert_node_data(self, option_path, option_data_dict):
        """
        update option_path leaf with option_data_dict
        insert branch if it doesn't exist
        clone attribute-set-of branch for new upsertions
        """
        if not self.tree.get_node(option_path):
            # Insert Attribute([]), then Attribute('foo'), then Attribute('foo.bar') into tree
            parent_option_path = Attribute([])
            for option_path_key in option_path:
                child_option_path = Attribute.from_insertion(parent_option_path, option_path_key)
                if child_option_path not in self.tree:
                    # copy attribute-set-of spec to new branch if branch doesn't yet exist
                    if self._is_attribute_set(parent_option_path) and child_option_path.get_end() != '<name>':
                        new_branch = self._get_attribute_set_template_branch(child_option_path)
                        self.tree.paste(parent_option_path, new_branch)
                    else:
                        self.tree.create_node(
                            child_option_path,
                            child_option_path,
                            parent=parent_option_path,
                            data=OptionData(_type='attribute set')
                        )
                parent_option_path = child_option_path

        option_data = self.tree.get_node(option_path).data or OptionData()
        option_data.update(option_data_dict)
        self.tree.update_node(option_path, data=option_data)

    def _is_attribute_set(self, attribute):
        data = self.tree.get_node(attribute).data
        if data:
            return 'attribute set of' in data._type  # TODO https://github.com/nix-gui/nix-gui/issues/65
        return False

    def _get_attribute_set_template_branch(self, attribute):
        """
        AttributeSetOf type attribute paths are parents of attribute paths which inherit their OptionData
        This method constructs a new branch for `attribute` labelled with the OptionData from its parent
        """
        parent_attribute = attribute.get_set()
        data = self.tree.get_node(parent_attribute).data  # get spec of parent attribute
        if data._type == 'attribute set of submodules':
            submodule_spec_attribute = Attribute.from_insertion(parent_attribute, '<name>')
            tree = Tree(self.tree.subtree(submodule_spec_attribute), deep=True)
            # <name> -> actual attribute
            for node in tree.all_nodes():
                attr = node.identifier
                loc = list(attr.loc)
                loc[loc.index('<name>')] = attribute.loc[-1]
                new_attr = Attribute(loc)
                tree.update_node(attr, identifier=new_attr, tag=new_attr)
            return tree
        else:
            option_data_spec = data.copy()
            # TODO https://github.com/nix-gui/nix-gui/issues/65
            option_data_spec._type = data._type.removeprefix('attribute set of ').removesuffix('s')
            tree = Tree()
            node = Node(attribute, attribute, data=option_data_spec)
            tree.add_node(node)
            return tree

    def _get_data(self, attribute):
        if self.tree.get_node(attribute).data is None:
            raise ValueError()
        return self.tree.get_node(attribute).data

    def iter_changes(self, get_configured_changes=False):
        if get_configured_changes:
            change_cache = self.configured_change_cache
        else:
            change_cache = self.in_memory_change_cache
        for attr, new_definition in change_cache.items():
            old_definition = self.get_definition(
                attr,
                include_in_memory_definition=False,
                include_configured_change=not get_configured_changes
            )
            if new_definition != old_definition:
                yield (attr, old_definition, new_definition)

    @functools.lru_cache()
    def get_change_set_with_ancestors(self, get_configured_changes=False):
        attributes_with_mutated_descendents = set()
        for attr, old_d, new_d in self.iter_changes(get_configured_changes):
            for i in range(len(attr)):
                attributes_with_mutated_descendents.add(attr[:i])
        return attributes_with_mutated_descendents

    def iter_attribute_data(self):
        for node in self.tree.all_nodes():
            if '<name>' not in node.tag:
                yield (node.identifier, node.data)

    def iter_attributes(self):
        for attr, _ in self.iter_attribute_data():
            yield attr

    def insert_attribute(self, attribute):
        self._upsert_node_data(attribute, {})

    def rename_attribute(self, old_attribute, new_attribute):
        self.tree.update_node(old_attribute, identifier=new_attribute, tag=new_attribute)
        for node in self.tree.children(new_attribute):
            old_child_attribute = node.identifier
            new_child_attribute = Attribute.from_insertion(new_attribute, old_child_attribute.get_end())
            self.rename_attribute(old_child_attribute, new_child_attribute)

    def set_definition(self, option_path, option_definition):
        self._upsert_node_data(option_path, {'in_memory_definition': option_definition})
        self._update_in_memory_change_cache(option_path, option_definition)

    def get_definition(self, attribute, include_in_memory_definition=True, include_configured_change=True):
        if include_in_memory_definition:
            in_memory_definition = self.get_in_memory_definition(attribute)
            if in_memory_definition != OptionDefinition.undefined():
                return self.get_in_memory_definition(attribute)

        if include_configured_change:
            configured_definition = self.get_configured_definition(attribute)
            if configured_definition != OptionDefinition.undefined():
                return configured_definition

        system_default_definition = self.get_system_default_definition(attribute)
        if system_default_definition != OptionDefinition.undefined():
            return system_default_definition

        return OptionDefinition.undefined()

    def get_in_memory_definition(self, attribute):
        return self._get_data(attribute).in_memory_definition

    def get_configured_definition(self, attribute):
        return self._get_data(attribute).configured_definition

    def get_system_default_definition(self, attribute):
        return self._get_data(attribute).system_default_definition

    def get_type(self, attribute):
        return self._get_data(attribute)._type

    def get_description(self, attribute):
        return self._get_data(attribute).description

    def is_readonly(self, attribute):
        return self._get_data(attribute).readOnly

    def children(self, attribute, mode="direct"):
        """
        attribute: the key to explore children of
        mode:
        - "direct": get direct descendents
        - "full": get all descendents
        - "leaves": get only descendents which have no children
        """
        try:
            if mode == "direct":
                children = self.tree.children(attribute)
            elif mode == "leaves":
                children = self.tree.leaves(attribute)
            else:
                raise ValueError()
        except treelib.exceptions.NodeIDAbsentError:
            raise ValueError()
        return {
            node.tag: node.data
            for node in children
            if '"<name>"' not in node.tag
        }

    def get_next_branching_option(self, attribute):
        while len(self.children(attribute)) == 1:
            attribute = self.children(attribute)[0]
        return attribute
