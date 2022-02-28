SHELL := /bin/bash
.PHONY: unit-tests integration-tests test smoke-test-dev-env

unit-tests:
	rm -rf tests/output
	# Install collection(s) that we depend upon during test
	ansible-galaxy collection install -p ../../ community.internal_test_tools
	ansible-test units --python 3.8 --docker

integration-tests:
	ansible-test integration --docker

test: unit-tests integration-tests

smoke-test-dev-env:
	# Ensure we are working from ansible_collections/ansible/automation_platform_installer
	@if [ `basename ${PWD}` != "automation_platform_installer" ]; then \
		printf "Error: expected current directory to be 'automation_platform_installer'. Instead it is '`basename ${PWD}`'. Please read CONTRIBUTING.md"; \
		exit 2; \
	else true; fi
	# TODO: Make sure ../ansible and ../../ansible_collections
	ANSIBLE_COLLECTIONS_PATHS=../../ ansible-doc -l ansible.automation_platform_installer | grep "calculate_mesh"
	@echo -e "\nYour development environment is setup correctly!"
