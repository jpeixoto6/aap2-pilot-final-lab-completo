#!/usr/bin/python
# Make coding more python3-ish, this is required for contributions to Ansible
from __future__ import absolute_import, division, print_function

__metaclass__ = type

from collections import defaultdict, deque

from ansible.errors import AnsibleError
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.plugins.action import ActionBase


_CONTROL_PLANE = "automationcontroller"
_EXECUTION_PLANE = "execution_nodes"
_INSTANCE_GROUP = 'instance_group'
_DEPROVISION = 'deprovision'

_NODE_VALID_TYPES = {
    _CONTROL_PLANE: {
        "types": frozenset(("control", "hybrid")),
        "default_type": "hybrid",
    },
    _EXECUTION_PLANE: {
        "types": frozenset(("execution", "hop")),
        "default_type": "execution",
    },
    _INSTANCE_GROUP: {
        "types": frozenset(("execution", "hybrid")),
        "default_type": "execution",
    },
}
_NODE_EXECUTABLE_TYPES = ('hybrid', 'execution')

_REQUIRED_ATTRIBUTES = {
    'receptor_log_level': 'info',
    'receptor_control_service_name': 'control',
    'receptor_control_filename': 'receptor.sock',
    'receptor_listener_port': 27199,
    'receptor_listener_protocol': 'tcp',
    'node_state': 'active',
}
_GENERATE_DOT_FILE_PARAM = "generate_dot_file"


class ActionModule(ActionBase):

    def generate_control_plane_topology(self, data):
        res = defaultdict(set)
        control_plane = [node for node in data['groups'].get(_CONTROL_PLANE, [])
                         if data['hostvars'][node].get('node_state') != _DEPROVISION]
        for index, control_node in enumerate(control_plane, 1):
            res[control_node] |= set(control_plane[index:])
        return res

    def connect_peers(self, data):
        res = defaultdict(set)

        for group_name in _NODE_VALID_TYPES:
            for node in data["groups"].get(group_name, []):
                if data['hostvars'][node].get('node_state') == _DEPROVISION:
                    continue
                res[node]  # populate with an empty set
                peers = data["hostvars"][node].get("peers", "")
                if isinstance(peers, (str, bytes)):
                    peers = [peer for peer in (p.strip() for p in peers.split(',')) if peer]
                for peer in peers:
                    # handle groups
                    if peer in data["groups"]:
                        # set comprehension to produce peers list. excludes circular reference to node
                        res[node] |= {x for x in data["groups"][peer] if x != node}
                    else:
                        res[node].add(peer)

                # filter back out the deprovisioned nodes from the peers
                res[node] = {peer for peer in res[node]
                             if peer not in data['hostvars'] or
                             data['hostvars'][peer].get('node_state') != _DEPROVISION}

        return res

    def deep_merge_dicts(self, *args):

        data = defaultdict(set)

        for d in args:
            for k, v in d.items():
                data[k] |= set(v)

        return dict(data)

    def assert_no_dangling_peers(self, data):
        for node, peers in data.items():
            for peer in peers:
                if peer not in data:
                    raise AnsibleError(f"Cannot have a peer to a node that doesn't exist: {peer}")

    def assert_no_self_peering(self, data):
        conflicts = set()
        for node, peers in data.items():
            if any(host == node for host in peers):
                conflicts.add(node)
        if conflicts:
            conflict_str = ', '.join(conflicts)
            raise AnsibleError(f"Cannot have nodes peering to themselves: {conflict_str}")

    def assert_no_2cycles(self, data):
        conflicts = set()
        for node, peers in data.items():  # node = host, peers = set(hosts)
            for host in peers:
                if node in data.get(host, set()):
                    conflicts.add(frozenset((node, host)))
        if conflicts:
            conflict_str = ", ".join(f"[{n1}] <-> [{n2}]" for n1, n2 in conflicts)
            raise AnsibleError(f"Two-way link(s) detected: {conflict_str} - Cannot have an inbound and outbound connection between the same two nodes")

    def assert_no_disconnected(self, data, root):
        if root is None:
            raise AnsibleError("There were no automationcontroller nodes found")

        undirected = defaultdict(set)
        # turn our directed graph data structure into an undirected graph
        for node, peers in data.items():
            undirected[node] |= peers
            for p in peers:
                undirected[p].add(node)

        # breadth-first search starting with our control-plane node
        seen = {root,}
        queue = deque(seen)
        while queue:
            v = queue.popleft()
            new_peers = undirected[v] - seen
            queue.extend(new_peers)
            seen |= new_peers

        if set(undirected) - seen:
            disconn = ', '.join(set(undirected) - seen)
            raise AnsibleError(
                f"There are receptor nodes that do not have a path back to the control plane: {disconn}"
            )

    def assert_autodisable_listener_without_incoming(self, task_vars, data):
        nodes = set(data)

        incoming = {p for peers in data.values() for p in peers}
        # A conflict only happens when `receptor_listener` is explicitly set False, but the node has incoming.
        conflicts = {
            node for node in incoming
            if not boolean(task_vars['hostvars'][node].get('receptor_listener', True), strict=False)
        }
        if conflicts:
            confl = ', '.join(conflicts)
            raise AnsibleError(
                f"There are receptor nodes that have the listener turned off that have incoming peers: {confl}"
            )

        # A node is disabled if it has no incoming and it does not have `receptor_listener` explicitly set True.
        return {
            node for node in nodes - incoming
            if not boolean(task_vars['hostvars'][node].get('receptor_listener', False), strict=False)
        }

    def assert_node_type(self, host, vars, group_name, valid_types):
        """
        Members of a given inventory group must have a valid node_type.
        """
        if "node_type" not in vars:
            return valid_types[group_name]["default_type"]

        if vars["node_type"] not in valid_types[group_name]["types"]:
            valid = ', '.join(str(t) for t in valid_types[group_name]['types'])
            raise AnsibleError(
                f"Receptor node {host} has an invalid node_type for group {group_name}, it must be one of the following: {valid}"
            )
        return vars["node_type"]

    def assert_attributes(self, vars, attr):
        """
        Assert if a required attribute is present
        otherwise return with a default value.
        """
        if attr not in vars:
            return _REQUIRED_ATTRIBUTES[attr]
        return vars[attr]

    def assert_unique_group(self, task_vars):
        """
        A given host cannot be part of the automationcontroller and execution_nodes group.
        """
        automation_group = task_vars["groups"].get(_CONTROL_PLANE)
        execution_nodes = task_vars["groups"].get(_EXECUTION_PLANE)

        if automation_group and execution_nodes:
            intersection = set(automation_group) & set(execution_nodes)
            if intersection:
                raise AnsibleError(
                    "The following hosts cannot be members of both [automationcontroller] and [execution_nodes]"
                    " groups: {0}".format(
                        ", ".join(str(i) for i in intersection)
                    )
                )

    def assert_minimal_active_nodes(self, task_vars):
        """
        Ensure automationcontroller[0] node has node_state=active
        for guarantee workable nodes
        """
        first_controller_node = task_vars["groups"][_CONTROL_PLANE][0]
        first_controller_node_state = task_vars["hostvars"][first_controller_node].get("node_state")

        if first_controller_node_state == _DEPROVISION:
            raise AnsibleError(
                "Deprovisioning the first member from the automationcontroller group is not allowed. "
                "Ensure the 'node_state=active' is set to the host '{0}'".format(first_controller_node)
            )

    def assert_some_execution_capable_nodes(self, data):
        if not any(attrs['node_type'] in _NODE_EXECUTABLE_TYPES for attrs in data.values()):
            raise AnsibleError(
                "There are no nodes of a type (hybrid or execution) capable of"
                " executing user jobs in your inventory."
            )

    def assert_control_only_nodes(self, task_vars, data):
        """
        Assert instance_group_* members are not present in the automationcontroller group
        and has a valid type.
        """
        for group, members in ((g, m) for g, m in task_vars["groups"].items() if g.startswith(_INSTANCE_GROUP)):
            for host in members:
                if task_vars['hostvars'][host].get('node_state') == _DEPROVISION:
                    continue

                if host not in data:
                    raise AnsibleError(
                        "The host '{0}' is not present in either [automationcontroller] or [execution_nodes]".format(host)
                    )

                if data[host]["node_type"] not in _NODE_VALID_TYPES['instance_group']["types"]:
                    raise AnsibleError(
                        "The host '{0}' is a member of the group '{1}', "
                        "its node_type must be one of these types: {2}".format(
                            host,
                            group,
                            ", ".join(str(i) for i in _NODE_VALID_TYPES['instance_group']["types"])),
                    )

    def write_dot_graph_to_file(self, control_nodes, all_nodes, filename="mesh.dot"):

        if not filename:
            return

        with open(filename, mode='w') as f:
            # Write the header and subgraph header
            f.write(
                'strict digraph "" {\n'
                '    rankdir = TB\n'
                '    node [shape=box];\n'
                '    subgraph cluster_0 {\n'
                '        graph [label="Control Nodes", type=solid];\n'
                '        {\n'
                '            rank = same;\n'
            )

            # Write out each control node
            for node in control_nodes:
                f.write(f'            "{node}";\n')

            for node, peers in control_nodes.items():
                for peer in peers:
                    f.write(f'            "{node}" -> "{peer}";\n')

            f.write(
                "        }\n"
                "    }\n\n"
            )

            # Write out each execution node
            for node in set(all_nodes) - set(control_nodes):
                f.write(f'    "{node}";\n')

            for node, peers in all_nodes.items():
                for peer in peers:
                    f.write(f'    "{node}" -> "{peer}";\n')

            f.write("}\n")

    def run(self, tmp=None, task_vars=None):

        if task_vars is None:
            raise AnsibleError("task_vars is blank")

        super(ActionModule, self).run(tmp, task_vars)

        # generate a dict of peers connectivity
        control_nodes = self.generate_control_plane_topology(task_vars)
        all_nodes = self.connect_peers(task_vars)
        peers = self.deep_merge_dicts(control_nodes, all_nodes)

        try:
            self.assert_no_dangling_peers(peers)
            # detect cycles; fail gracefully if found
            self.assert_no_self_peering(peers)
            self.assert_no_2cycles(peers)

            # detect disconnected subgraphs
            base_control = next(iter(control_nodes or [None]))  # pick an arbitrary control-plane node
            self.assert_no_disconnected(peers, base_control)

            # find the listener-disabled nodes
            disabled = self.assert_autodisable_listener_without_incoming(task_vars, peers)

            # create a skeleton return object and fill it in with peers and node_type
            data, deprovision = {}, {}
            for group in (_CONTROL_PLANE, _EXECUTION_PLANE):
                for host in task_vars["groups"].get(group, []):
                    _host_vars = dict(task_vars["hostvars"][host])
                    myhost_data = {}
                    myhost_data["peers"] = sorted(peers.get(host, []))
                    myhost_data["node_type"] = self.assert_node_type(
                        host=host,
                        vars=_host_vars,
                        group_name=group,
                        valid_types=_NODE_VALID_TYPES,
                    )

                    # ensure all receptor attributes are set
                    for attr in _REQUIRED_ATTRIBUTES:
                        myhost_data[attr] = self.assert_attributes(vars=_host_vars, attr=attr)

                    myhost_data['receptor_listener'] = host not in disabled

                    dataset = data if host in peers else deprovision
                    dataset[host] = myhost_data

            groups = {
                group: [node for node in members if node in data]
                for group, members in task_vars['groups'].items()
            }

            # ensure that no nodes are shared between automationcontroller and execution_nodes
            self.assert_unique_group(task_vars)

            # ensure that there exists at least one hybrid or execution node
            self.assert_some_execution_capable_nodes(data)

            # ensure automationcontroller[0] has node_state=active for minimum quorum
            self.assert_minimal_active_nodes(task_vars)

            # ensure instance_group_* members are not control only
            self.assert_control_only_nodes(task_vars, data)

        finally:
            # generate dot file if user expressed interest in doing so
            if _GENERATE_DOT_FILE_PARAM in self._task.args:
                self.write_dot_graph_to_file(control_nodes, all_nodes,
                                             self._task.args[_GENERATE_DOT_FILE_PARAM])

        return dict(mesh=data, deprovision_mesh=deprovision,
                    mesh_groups={group: members for group, members in groups.items() if members})
