# Copyright (c) 2017 Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible.errors import AnsibleFilterError

_RECEPTORCTL_TASK_ATTRS = ['results', 'changed']
_RECEPTORCTL_CMD_PARAMS = ['receptorctl', '--socket', 'ping']

__ERROR_MSG = "Not a valid 'receptorctl ping' task. Cannot process value"

def _assert_receptorctl_task(task_output):

    # verify if task_output is a dict
    if not isinstance(task_output, dict):
        raise AnsibleFilterError(__ERROR_MSG)

    # verify if required task_output attributes are present
    if not all(item in task_output for item in _RECEPTORCTL_TASK_ATTRS):
        raise AnsibleFilterError(__ERROR_MSG)

    # verify if receptorctl is present in the cmd
    for item in task_output.get('results', []):
        if not all(param in item.get('cmd', []) for param in _RECEPTORCTL_CMD_PARAMS):
            raise AnsibleFilterError(__ERROR_MSG)


def do_mesh_report(task_output):

    # validate task format
    _assert_receptorctl_task(task_output)

    successful = []
    failed = []
    for cmd in task_output.get('results', []):
        cmd_stdout = cmd.get('stdout') or ''
        node = cmd.get('item')
        if cmd_stdout.startswith('Reply from') and node:
            successful.append(node)
        else:
            failed.append(node)

    return dict(mesh_successful_pings=successful, mesh_failed_pings=failed)


class FilterModule(object):
    ''' Ansible Receptor Mesh '''

    def filters(self):
        return {
            'mesh_report': do_mesh_report,
        }
