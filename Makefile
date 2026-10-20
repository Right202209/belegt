PKGS := belegt-dictionary belegt-calc belegt-core belegt-io belegt-supplier belegt-connectors belegt-bench
.PHONY: install test test-web demo serve bench clean
install:
	pip install -e packages/belegt-dictionary -e packages/belegt-calc -e "packages/belegt-core[all]" \
	  -e packages/belegt-io -e packages/belegt-supplier -e packages/belegt-connectors -e packages/belegt-bench
test:
	@set -e; for p in $(PKGS); do echo "== $$p"; (cd packages/$$p && python -m pytest -q); done
test-web:
	cd web && npm ci && npm test && npm run build
demo:
	belegt demo --out out/demo
serve:
	BELEGT_DEV_AUTH=1 belegt serve --dev-auth
bench:
	belegt-bench run --agent question-parser && belegt-bench gate
clean:
	find . -name __pycache__ -o -name '*.egg-info' -o -name .pytest_cache | xargs rm -rf; rm -rf web/node_modules web/dist out
