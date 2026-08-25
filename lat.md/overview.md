# System Intent

ggbuild turns native package recipes into compatible, reproducible artifacts across operating systems while preserving each platform's packaging conventions.

The central problem is not merely compilation. A useful result must resolve system and bundled dependencies, install into the right layout, remain relocatable where promised, and carry enough provenance to test and publish safely.

## Two-Layer Architecture

ggbuild separates the mechanics of building one recipe from the orchestration of many immutable build nodes.

The low-level engine resolves dependencies for the actual target, renders recipe hooks into shell programs, and delegates packaging to portable archive or native package builders. See [[recipes#Dependency Semantics]] and [[packaging#Target Abstraction]].

The project layer selects registered releases, creates a canonical dependency graph, assigns content identities, and runs the same nodes locally or in CI. It deliberately re-enters the low-level engine rather than maintaining a second packaging implementation; see [[orchestration#Low-Level Build Boundary]].

## Reproducibility Model

Reproducibility comes from explicit identities and fail-closed validation, not from assuming that a repeated command sees unchanged inputs.

Source archives are tied to digests, recipe directories and target environments enter cache keys, editable ggbuild checkouts include Git and dirty-tree identity, and serialized plans are canonicalized. Build timestamps remain metadata, not cache identity.

This makes a cache hit a claim about all declared inputs. [[orchestration#Canonical Build Plan#Cache Identity]] describes the exact boundary; [[packaging#Validated Resume]] covers reuse of a retained low-level work tree.

## Trust Boundaries

Files crossing process, cache, archive, or CI-job boundaries are treated as data requiring structural and content validation.

Downloaded sources are verified before reuse, bundles and test artifacts reject escaping paths and symlinks, dependency trees merge without silent overwrites, and publication reconstructs canonical records from bytes on disk. See [[recipes#Verified Source Acquisition]], [[orchestration#Bundle Validation and Merge]], and [[artifacts#Complete Snapshot Publication]].

## Compatibility Strategy

Compatibility is established by building against deliberate platform baselines and inspecting the produced binary closure.

Portable Linux builds use pinned GNU or musl roots; macOS records a deployment floor; native Debian and RPM paths use their package managers. Generic outputs rewrite runtime paths and reject undeclared non-system shared libraries. Linux tests then run outside the rich build image at the ABI floor. See [[packaging#Portable and Native Outputs]] and [[artifacts#ABI-Floor Linux Tests]].
