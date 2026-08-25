# ggbuild Knowledge Graph

This graph records why ggbuild is structured as a recipe-driven packaging engine plus a reproducible project orchestrator.

- [[overview]] explains the problem boundary and the architectural split.
- [[recipes]] covers immutable releases, dependency meaning, and trustworthy source acquisition.
- [[packaging]] describes build execution, portability, resumability, and artifact assembly.
- [[orchestration]] defines canonical plans, bundles, scheduling, containers, and generated CI.
- [[artifacts]] follows artifacts through testing and all-or-nothing publication.
- [[maintenance]] explains declarative updates and version-aware patch rerolling.

The implementation is Python 3.14+, with its public CLI rooted at [[src/ggbuild/app.py#main]]. Source `@lat:` comments point back to the design sections that explain non-obvious behavior.
