.PHONY: help ruff
.DEFAULT_GOAL := help

help: ## Display this help message
	@echo "Available commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

ruff: ## Run ruff linter
	ruff check run_qemu_vm
