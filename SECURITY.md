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

## What Crystal isolates, and what it does not

Since Build 017, every external tool Crystal launches gets an **allowlisted**
environment rather than the operator's own. This closes a real exposure:
Foundry hands a Solidity harness `vm.envUint("PRIVATE_KEY")`, and that harness
is compiled from a contract Crystal did not write, so a deploy key or an RPC
URL carrying an API token was readable from inside the target's own code.
Backends already ran in a disposable working directory, and the generated
`foundry.toml` now states `ffi = false` rather than relying on Foundry's
default.

**Crystal does not sandbox the process.** Tools run as you, on your host, with
your rights. `crystal doctor` prints exactly this, and the same report travels
in every scan payload. If you are analysing something you believe to be
hostile, run Crystal inside a disposable machine — that is the layer Crystal
does not provide and does not claim to.

A credential reaching a launched tool *is* in scope, and is what the
`invariants` CI gate exists to prevent regressing.

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
