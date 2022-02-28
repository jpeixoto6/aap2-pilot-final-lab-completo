from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

DOCUMENTATION = """
---

module: upgrade_isolated_inventory
short_description: Controller 3.8.x to >=4.1.x isolated inventory upgrade
version_added: "1.0.0"
description: Given an inventory file to ansible-playbook, migrate the old isolated groups to new execution_nodes
options:
    output_inventory_path:
        description: path(s) to output new migrated inventory
        default: {inventory_dir | playbook_dir}/inventory.new.ini
        required: False
        type: str
author:
    - Rebeccah Hunter <rhunter@redhat.com>
    - Jeff Bradberry <jbradber@redhat.com>
"""

EXAMPLES = r'''
- name: Migrate 3.8.x inventory to 4.1.x
  ansible.automation_platform_installer.upgrade_isolated_inventory:

- name: Migrate 3.8.x inventory to 4.1.x
  ansible.automation_platform_installer.upgrade_isolated_inventory:
    output_inventory_path: /var/log/controller/4.1/inventory
'''

RETURN = r'''
'''

import os
import json
from copy import deepcopy
import itertools
from collections import defaultdict
from ansible.errors import AnsibleError
from ansible.plugins.action import ActionBase

ansible_vars = {
    "ansible_facts", "ansible_playbook_python", "ansible_version", "ansible_check_mode",
    "ansible_config_file", "ansible_diff_mode", "ansible_forks", "ansible_inventory_sources",
    "ansible_skip_tags", "ansible_run_tags", "ansible_verbosity", "groups", "group_names",
    "inventory_file", "inventory_dir", "inventory_hostname", "inventory_hostname_short",
    "playbook_dir", "omit"
}


class ActionModule(ActionBase):
    def check_for_old_format(self, task_vars):
        # if it's old do stuff, if it's not, pass
        for group in task_vars["groups"]:
            if group == "tower":  # check for tower, which will become automationcontroller
                return True
            if group.startswith("isolated_group_"):  # check if group is isolated
                return True
        for attr in task_vars["hostvars"].values():
            if "controller" in attr:
                return True
        return False

    def construct_new_format(self, task_vars):
        # if there was an exception with the original inventory file, attempt to convert it.
        # should return a dict of dicts like this:
        #   {'groupname': {'hosts': [...], 'children': [...], 'vars': {...}, ...}}

        # build up a mapping of variable-value pairs to the set of hosts that have each value
        var_hosts = defaultdict(set)
        for host, attrs in deepcopy(task_vars['hostvars']).items():  # make sure inner data structures are safe
            for key, value in attrs.items():
                if key not in ansible_vars:
                    try:
                        var_hosts[(key, value)].add(host)
                    except TypeError:  # `value` is unhashable, and thus probably came in from a vars file
                        pass

        # create a nested dictionary of group membership information
        new_groups = defaultdict(lambda: {"all_hosts": [], "hosts": [], "children": [], "vars": {}})
        for group, members in task_vars['groups'].items():
            if group == "tower":
                # tower becomes automationcontroller in 4.X
                group = "automationcontroller"
                new_groups[group]["tower-group-rename"] = True

            elif group.startswith("isolated_group_"):
                # convert the old controller variable on this isolated group into a
                # peers variable that gets placed in the group that was originally the controller
                controller = task_vars['hostvars'][members[0]].get("controller", "automationcontroller")
                var_hosts[("controller", controller)] -= set(members)
                controller = f"instance_group_{controller}" if controller != "tower" else "automationcontroller"
                new_groups[controller]["peer-conversion"] = True

                # convert the isolated group into an instance group
                group = group.replace("isolated_group_", "instance_group_")
                new_groups[group]["iso-group-rename"] = True
                new_groups[group]['vars']['node_state'] = 'iso_migrate'
                # set a peering connection based on `controller=`
                new_groups[controller].setdefault("peers", []).append(group)

                # initialize the new execution nodes supergroup and then populate it
                enodes_data = new_groups['execution_nodes']
                enodes_data['execution-nodes-group'] = True
                enodes_data['children'].append(group)
                enodes_data['all_hosts'].extend(m for m in members if m not in enodes_data['all_hosts'])

            elif group == 'ungrouped':
                new_groups[group]['hosts'].extend(members)

            new_groups[group]['all_hosts'].extend(members)

        # look at the groups from order largest to smallest and distribute
        # _any_ hostvars that are set on every host in the group into a group variable
        for group, group_data in sorted(new_groups.items(), key=lambda x:len(x[1]["all_hosts"]), reverse=True):
            if group == "ungrouped":
                continue
            if 'peers' in group_data:
                group_data['vars']['peers'] = ','.join(group_data['peers'])
            for (key, value), hosts in var_hosts.items():
                if group_data["all_hosts"] and hosts >= set(group_data["all_hosts"]):
                    group_data["vars"][key] = value
                    hosts -= set(group_data["all_hosts"])

        # Any vars left over should be set directly on the host
        new_vars = defaultdict(dict)
        for (key, value), hosts in var_hosts.items():
            for host in hosts:
                new_vars[host][key] = value

        # Ansible throws away parent-child group information, just
        # fully expanding out each group into the set of hosts in the
        # group.  We need to try to reconstruct a set of child groups
        # while minimizing redundancy.

        # I. detect which groups are subsets of other groups
        #    and create a data structure to reference in part II.
        user_groups = set(new_groups) - {'all', 'ungrouped', 'execution_nodes'}
        subgroups = defaultdict(list)
        for group1, group2 in itertools.combinations(user_groups, 2):
            members1 = set(new_groups[group1]['all_hosts'])
            members2 = set(new_groups[group2]['all_hosts'])
            if len(members1) <= 1 or len(members2) <= 1:
                continue
            if members1 < members2 and group1 != "automationcontroller":
                subgroups[group2].append(group1)
            elif members2 < members1 and group2 != "automationcontroller":
                subgroups[group1].append(group2)

        # II. fold hosts into children groups as long as a
        #     larger child group has not already been chosen
        for group in user_groups:
            children = set(subgroups[group])
            for sub in subgroups[group]:
                if sub in children:
                    children -= set(subgroups[sub])
            new_groups[group]['children'] = [s for s in subgroups[group] if s in children]
            child_hosts = {m for sub in new_groups[group]['children'] for m in new_groups[sub]['all_hosts']}
            new_groups[group]['hosts'] = [m for m in new_groups[group]['all_hosts'] if m not in child_hosts]
        return new_groups, new_vars

    def write_new_format(self, inv_output_path, new_groups, host_vars):
        # create a formatted string for each section
        # within the final contents of the ini file
        blocks = []
        for group, group_data in new_groups.items():
            if group == 'execution_nodes' or (group_data['hosts'] and group != 'all'):
                block = []
                if group_data.get("tower-group-rename"):
                    block.append(
                        "# In AAP 2.X [tower] has been renamed to [automationcontroller]\n"
                        "# Nodes in [automationcontroller] will be hybrid by default, capable of executing user jobs.\n"
                        "# To specify that any of these nodes should be control-only instead, give them"
                        " a host var of `node_type=control`\n"
                    )
                elif group_data.get("iso-group-rename"):
                    block.append(
                        "# in AAP 2.X isolated groups are no longer a special type, and should be "
                        "renamed to be instance groups\n"
                    )
                elif group_data.get("execution-nodes-group"):
                    block.append(
                        "# In AAP 2.X Execution Nodes have replaced isolated nodes."
                        " All of these nodes will be by default\n"
                        "# `node_type=execution`. You can specify new nodes that cannot execute jobs"
                        " and are intermediaries\n# between your control and execution"
                        " nodes by adding them to [execution_nodes] and setting a host var\n"
                        "# `node_type=hop` on them.\n"
                    )
                block.extend([f"[{group}]\n"] if group != 'ungrouped' else [])
                for member in group_data['hosts']:
                    attrs = host_vars.pop(member, {})
                    attrs_str = "".join(f" {k}={v!r}" for k, v in attrs.items())
                    block.append(f"{member}{attrs_str}\n")
                blocks.append(''.join(block))

            if group_data['children'] and group not in ('all', 'ungrouped'):
                block = [f"[{group}:children]\n"]
                for sub in group_data['children']:
                    block.append(f"{sub}\n")
                blocks.append(''.join(block))

            if group_data['vars'] and group != 'ungrouped':
                block = []
                block.append(f"[{group}:vars]\n")
                for k, v in group_data['vars'].items():
                    if group_data.get("peer-conversion") and k == "peers":
                        block.append(
                            "# in AAP 2.X the controller variable has been replaced with `peers`\n"
                            "# which allows finer grained control over node communication.\n"
                            "# `peers` can be set on individual hosts, to a combination of multiple groups and hosts.\n"
                        )
                    if group_data.get('iso-group-rename') and k == 'node_state':
                        block.append(
                            "# in AAP 2.X Isolated Nodes are converted into Execution Nodes using"
                            " node_state=iso_migrate\n"
                        )
                    block.append(f"{k}={v!r}\n")
                blocks.append(''.join(block))

        # finally, write the file
        with open(inv_output_path, "w") as inv:
            inv.write('\n'.join(blocks))

    def run(self, tmp=None, task_vars=None):
        if task_vars is None:
            raise AnsibleError("task_vars is blank")

        super(ActionModule, self).run(tmp, task_vars)

        inv_output_path = self._task.args.get('output_inventory_path', None)

        if inv_output_path:
            inv_output_path = os.path.abspath(inv_output_path)
        else:
            localhost_vars = task_vars['hostvars']['localhost']
            output_dir = localhost_vars.get('ansible_inventory_sources', [])
            if len(output_dir) > 0:
                output_dir = os.path.dirname(output_dir[0])
            else:
                output_dir = localhost_vars.get('playbook_dir', None)

            if not output_dir:
                output_dir = './'
            inv_output_path = os.path.join(output_dir, 'inventory.new.ini')

        if self.check_for_old_format(task_vars):
            new_groups, new_vars = self.construct_new_format(task_vars)
            self.write_new_format(inv_output_path, new_groups, new_vars)
            return {
                "failed": True,
                "msg": (
                    "The installer has detected that you are using an inventory format from "
                    "a version prior to 4.0. We have created an example inventory based on "
                    f"your old style inventory. Please check the file `{inv_output_path}` and "
                    "make necessary adjustments so that the file can be used by the installer."
                )
            }
        else:
            return {"changed": False, "msg": "Old style inventory not detected, continuing with install"}
