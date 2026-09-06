# Security policy

This policy covers vulnerabilities **in Crystal itself** — not in the contracts
or services you point it at.

## Reporting

Report privately to **admin@kairos-lab.org**, or through GitHub's private
vulnerability reporting on this repository. Please do not open a public issue
for a vulnerability.

Include the version (`crystal --version`), the command, and a reproduction if
you have one. You will get an acknowledgement; if the report is valid, you will
be told what the fix is and when it ships, and credited unless you ask
otherwise.

## What is in scope

Crystal reads untrusted input by design — sources you did not write, campaign
packs, deployment fixtures, backend output. Anything that turns reading into
executing is in scope:

* code execution while parsing a target;
* path traversal or writes outside the directories Crystal is told to use;
* a campaign pack or fixture escaping the analysis and affecting the host
  beyond the documented `--pack` / `--fixture` contract;
* leaking a target's contents somewhere the operator did not ask for.

**Note on `--pack` and `--fixture`:** both load and execute Python you supply.
That is deliberate and documented — a pack is code, like a pytest plugin. Do
not load one you have not read. A pack executing is not a vulnerability; a pack
executing *without* being passed on the command line would be.

## What is not a vulnerability

* **A wrong signal, or a missed defect.** Those are bugs — open a normal issue,
  they are the contributions we most want. See [CONTRIBUTING.md](CONTRIBUTING.md).
* **`UNSUPPORTED` or `VACUOUS` where you expected an answer.** Crystal declines
  rather than guessing; that is the design. If the reason it gives is wrong or
  missing, that is a bug worth reporting.
* **A backend behaving badly.** Report those to Foundry, Medusa, Halmos or
  Echidna. If Crystal *hides* such behaviour behind a green result, that is
  ours and we want to hear about it.

## Supported versions

Crystal is in beta. Only the latest build on `main` is supported; fixes ship
forward, not as patches to earlier builds.
