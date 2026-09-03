# Artifact Trust Pipeline

Artifacts advance from staged files to publication only through explicit inventories, target-compatible tests, and byte-bound success records.

## Artifact Inventory and Sidecars

Generic outputs pair distributions with metadata describing identity, target, source version, build details, and every emitted content file.

One primary artifact may be accompanied by a test-data overlay and debug-symbol archive. Sidecars have unique semantic roles rather than filename-only discovery; publication requires canonical zstd tar names for the public primary and dbgsym files.

Test data is staged separately from shipped runtime files, then passed through the same binary path-repair and closure checks before packaging. Debug symbols preserve artifact-relative paths while primary binaries are stripped.

## Recipe-Driven Artifact Tests

A test executes the exact hook of the registered recipe release named by artifact metadata, against an extracted completed artifact rather than a build tree.

Normal tests require an exact target/host match. The environment prepends artifact binaries and libraries, gives the hook an isolated home and package work directory, and exposes only root-package install and test-data paths through the `Test` API.

Archive extraction rejects traversal, unsafe links, hard links, devices, and unsupported members. A successful result deterministically binds recipe, source version, target, and the primary archive SHA-256; failures produce no reusable success.

## ABI-Floor Linux Tests

Portable Linux tests run in a minimal, read-only runtime container distinct from the build image to detect accidental ABI or toolchain dependencies.

The container has no network, drops capabilities, forbids privilege gain, uses a tmpfs for `/tmp`, and runs as a non-root identity. GNU tests use the oldest supported glibc root; musl tests use a deliberately minimal BusyBox/musl root.

This test permits the Docker host architecture to match while requiring the artifact OS, architecture, and libc semantics represented by its target and pinned test image.

## Test-to-Publication Binding

Each successful test creates a canonical root record proving which planned coordinate and artifact bytes passed.

The record verifies the test node's sole artifact dependency, recomputes the result and all file hashes, checks the metadata-declared inventory exactly, classifies public and private roles, and rejects unexpected, missing, duplicate, legacy, or noncanonical files.

Private metadata, test data, and test results remain in the snapshot manifest for provenance but are not uploaded as public distributions.

## Complete Snapshot Publication

Publication is all-or-nothing across every root coordinate in the canonical plan.

Before contacting GitHub, ggbuild validates every downloaded root record and requires the successful coordinate set to equal the planned roots exactly. It also verifies repository/run identity, release-wide names, hashes, sizes, and canonical public encodings.

The tag comes from the authoritative GitHub run start time at UTC minute precision. The publish job receives read-only Actions metadata access for that lookup; release writes and OIDC token minting remain separately scoped.

Any existing draft or published tag is rejected, so retries cannot mutate an ambiguous partial release.

## Ordered Release Commit

GitHub publication uses a draft as a transaction boundary and promotes it only after independently verifiable assets are complete.

Sorted public distributions upload first, then `SHA256SUMS`; ggbuild downloads that checksum asset and compares its bytes. The complete snapshot manifest uploads last, after which the draft becomes the latest immutable release.

If interrupted, the draft is intentionally preserved for operator inspection and cleanup. Optional downstream index ingestion is a separate OIDC-authenticated operation that can retry against an explicitly supplied immutable tag without rebuilding or changing the release.

After OIDC authentication, ingestion forwards the job's ephemeral GitHub token so private release assets can be read and rehashed. An ignored release is a workflow failure, preventing a transport-level success from concealing a missing index.
