# Grafana Cloud reference examples review

Each non-planned directory under `examples/` is a customer-downloadable bundle. Use
`example.yaml` for the runtime and layout contract. Check that the Terraform variable
and standalone CloudFormation parameter agree with it and that `tools/package_example.py`
can vendor each relative module and shared library into the zip. Keep example roots free
of a backend and templates independently deployable without nested stacks.

Review credential inputs, release contents and telemetry dimensions closely. Fetch
credentials at runtime and keep secret defaults empty across both infrastructure
implementations. Keep identifiers, request IDs, timestamps and object keys out of Loki
labels. Use the documented synthetic placeholders in templates, tests and examples.
A shared library change can affect every consumer. Check package boundaries and release
component scope in `release-please-config.json`.

`tools/check_conformance.py` already checks layout, runtime parity and template shape.
The main gate runs the publication scan and Python, Node and infrastructure checks.
Report packaging or runtime problems those checks miss, such as a dependency imported
from a workspace that is absent from the actual customer zip. Verify with `just check`
and `just package-all`; regenerate AEM dashboards through `just aem-dashboards`.
