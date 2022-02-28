
# The system UUID, as captured by Ansible.
SYSTEM_UUID = '{% if system_uuid is defined %}{{ system_uuid }}{% else %}{{ ansible_product_uuid.lower() }}{% endif %}'
