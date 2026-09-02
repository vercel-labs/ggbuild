# Release Maintenance

Release maintenance keeps recipe declarations and downstream patches synchronized with upstream versions without partially updating the repository.

## Declarative Release Discovery

Updateable recipes declare how to discover their complete desired registered-release set from HTTPS-only upstream sources.

Built-in policies support stable GitHub releases and version patterns in HTML indexes; recipes may override discovery to retain several supported major lines. Check mode reports drift without downloading archives, rerolling patches, or writing files.

New releases must resolve to one HTTPS archive. ggbuild downloads and hashes it, then rewrites standardized release constructor blocks so the source version and SHA-256 remain reviewable code.

## Transactional Recipe Updates

Release declarations and generated patch variants are collected in memory and replaced as one best-effort filesystem transaction.

Each file is written and fsynced through a same-directory temporary before replacement. If any replacement fails, already replaced files are restored in reverse order, preventing a recipe update from committing hashes without its corresponding patch changes.

## Version-Aware Patch Selection

Patch filenames encode logical identity separately from the version interval in which that implementation applies.

Variants may be unbounded, exact, half-open ranges, or legacy major/point suffixes. For each package and logical patch name, the unique narrowest matching interval wins; incomparable overlaps fail as ambiguous instead of depending on filename order.

This permits a broad patch to remain the default while a release-specific variant records an upstream conflict resolution. Build code receives only the selected logical series.

## Patch Rerolling

Rerolling preserves patch intent across upstream changes by reconstructing commits, not by blindly refreshing textual offsets.

ggbuild materializes the new verified release as a deterministic temporary Git repository and consults the recipe's canonical repository/ref. Patches that still apply within bounded fuzz are retained unchanged; already-upstream changes are rejected for manual range adjustment.

When a patch no longer applies, earlier registered releases in the same major line reconstruct its commit and Git three-way application carries that commit onto the new source. Changed content becomes an exact-version variant, preserving older variants for their original releases.

Conflicted worktrees are preserved with actionable Git diagnostics; successful temporary repositories are removed. All generated variants join the [[maintenance#Transactional Recipe Updates|same update transaction]] as release declarations.
