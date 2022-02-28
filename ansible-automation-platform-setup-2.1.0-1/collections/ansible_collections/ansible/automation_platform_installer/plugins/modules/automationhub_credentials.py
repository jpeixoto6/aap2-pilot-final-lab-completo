#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright: (c) 2021, Ansible Automation Platform
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

DOCUMENTATION = r'''
---
module: automationhub_credentials

short_description: Creates credentials for Automation Hub

version_added: "2.1.0"

description: Creates container registry and published/rh-certified/community collection credentials for Automation Hub and update credentials for execution environments

options:
    automationhub_url:
        description: Automation Hub Server URL
        required: true
        type: str
    username:
        description: Username for Automation Hub container registry
        required: true
        type: str
    password:
        description: Password for Automation Hub container registry
        required: true
        type: str
    token:
        description: Token for Automation Hub collection repository
        required: true
        type: str
    verify_ssl:
        description: Verify SSL when authenticating with the container registry
        required: true
        type: bool
    update_control_plane_cred:
        description: Update Control Plane Execution Environment to use image from Hub
        required: true
        type: bool

author:
    - Ansible Automation Platform Team
'''

from ansible.module_utils.basic import AnsibleModule

import os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "awx.settings.production")
django.setup()

from urllib.parse import urlsplit
from awx.main.models import CredentialType, Credential, Organization, ExecutionEnvironment


def run_module():
    module_args = dict(
        automationhub_url=dict(type='str', required=True),
        username=dict(type='str', required=True),
        password=dict(type='str', required=True, no_log=True),
        token=dict(type='str', required=True, no_log=True),
        verify_ssl=dict(type='bool', required=True),
        update_control_plane_cred=dict(type='bool', required=True),
    )

    result = dict(
        changed=False,
        message=[],
    )

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True
    )

    if module.check_mode:
        module.exit_json(**result)

    default_org = Organization.objects.filter(name='Default')
    cred_list = [
        {'repo': 'published', 'name': "Automation Hub Published Repository"},
        {'repo': 'rh-certified', 'name': "Automation Hub RH Certified Repository"},
        {'repo': 'community', 'name': "Automation Hub Community Repository"},
    ]

    # Create collection credentials and add to 'Default' organization
    for cred in cred_list:
        new_cred, cred_created = Credential.objects.get_or_create(
            name=cred['name'],
            credential_type=CredentialType.objects.get(kind='galaxy'),
            #organization=default_org,
            defaults={
                "inputs": {
                    "url": "%s/api/galaxy/content/%s/" % (module.params['automationhub_url'], cred['repo']),
                    "token": module.params['token'],
                },
            },
        )

        if default_org.exists():
            default_org.first().galaxy_credentials.add(new_cred)

        if cred_created:
            result['message'].append("Created '%s' credential." % cred['name'])

    if default_org.exists():
        galaxy_cred = Credential.objects.get(name="Ansible Galaxy")
        default_org.first().galaxy_credentials.remove(galaxy_cred)
        default_org.first().galaxy_credentials.add(galaxy_cred)

    # Create container registry credential
    registry_cred, cred_created = Credential.objects.get_or_create(
        name="Automation Hub Container Registry",
        credential_type=CredentialType.objects.get(kind='registry'),
        defaults={
            "inputs": {
                "host": urlsplit(module.params['automationhub_url']).hostname,
                "password": module.params['password'],
                "username": module.params['username'],
                "verify_ssl": module.params['verify_ssl'],
            },
        },
    )
    if cred_created:
        result['message'].append("Created 'Automation Hub Container Registry' credential.")

    # Update Hub EEs to use Hub registry credential
    ExecutionEnvironment.objects.filter(name__startswith="Automation Hub").update(credential=registry_cred)
    result['message'].append("Updated credential for Automation Hub Execution Environment images.")

    # Update Control Plane EE to use Hub credential
    if module.params['update_control_plane_cred']:
        ExecutionEnvironment.objects.filter(name="Control Plane Execution Environment").update(credential=registry_cred)
        result['message'].append("Updated credential for Control Plane Execution Environment.")

    result['changed'] = True
    module.exit_json(**result)


def main():
    run_module()


if __name__ == '__main__':
    main()

