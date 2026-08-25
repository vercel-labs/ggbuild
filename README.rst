=======
ggbuild
=======

ggbuild is a framework that allows building native packages and installers
for a variety of OSes and distributions in a way that ensures maximum
compatibility and integration with the target platform.

How it works
------------

ggbuild works by taking a package specification, generating a build script
that is most appropriate for the target platform, and then running the build
script directly on the target platform to produce the package artifacts.
For example, on RHEL targets ggbuild generates an RPM .spec file and the
associated files and runs ``rpmbuild`` to produce a well-behaved RPM package.
On Debian targets ``dpkg-buildpackage`` is used and so on.

ggbuild contains a builtin dependency resolver, based on
`Poetry <https://github.com/python-poetry/poetry>`_ that uses the native
platform's package manager to find the necessary dependencies.  This makes
it possible to use the libraries provided by the system and to avoid bundling
them.

Prerequisites
-------------

ggbuild requires Python 3.14+.

Installation
------------

To install, run ``pip install https://github.com/vercel-labs/ggbuild`` or clone
the repository and install an editable copy with ``pip install -e <checkout>``.

Development
-----------

Install the development environment with ``uv sync``, then install the managed
pre-commit and pre-push hooks with ``uv run poe setup``. Run the complete check
suite concurrently with ``uv run poe qa``; the individual ``lint``,
``typecheck``, and ``test`` tasks are also available.

Project orchestration
---------------------

ggbuild can plan and execute a complete project from registered recipe
releases and declarative policy in ``pyproject.toml``. It does not require a
Python project-provider plugin. A minimal project configuration looks like::

    [tool.ggbuild]
    root-recipe = "example.recipes:Example"
    release-selection = "latest-per-major"
    portable = true
    bundle-prefix = "example"

    [[tool.ggbuild.target]]
    triple = "x86_64-unknown-linux-gnu"

    [[tool.ggbuild.target]]
    triple = "aarch64-apple-darwin"

    [tool.ggbuild.workflow]
    name = "Package builds"
    path = ".github/workflows/build.yml"
    setup-action = "./.github/actions/build-setup"
    branch = "main"
    max-concurrency = 12
    artifact-name = "{package}-{target}-{version}"

    [tool.ggbuild.build]
    revision = "1"
    compression = ["zstd"]

    [tool.ggbuild.sccache]
    production = false
    pull-request = true

``release-selection`` accepts ``all``, ``latest``, or
``latest-per-major``. Targets use either the ``host`` or ``docker`` execution
mode. ggbuild derives execution mode, environment variables, and the Actions
runner from each target triple. For Docker targets, it also selects the
container platform, pinned base and UV images, target-owned template, and ABI
policy. A target entry may override ``execution``, ``runner``, or
``environment`` when needed. Workflow artifact naming is configured per
project.

Usage
-----

Build a configured project target, optionally selecting one registered
release::

    ggbuild build --target x86_64-unknown-linux-gnu
    ggbuild build --target x86_64-unknown-linux-gnu --source-ref 17.6

The original low-level interface remains available when no project target is
given::

    ggbuild build example.recipes:Example --enable-sccache

The CI commands create and execute plans::

    ggbuild ci plan --output plan.json
    ggbuild ci run --dry-run
    ggbuild ci execute-node --plan plan.json --node NODE_ID \
        --bundle-dir .cache --work-dir .cache/work \
        --output-dir dist

``ci run`` schedules dependency layers locally with bounded parallelism and
uses validated content-addressed bundles from ``.cache``. Generated GitHub
Actions workflows assign every plan node to its own matrix child and cap each
topological layer with ``max-concurrency``. The ``ci execute-node`` command
executes one root and its dependency closure; generated jobs use ``--exact``
to execute only their assigned node.

Editable ggbuild installations contribute the installed version, Git HEAD,
and a digest of tracked modifications plus untracked, non-ignored files to
every node cache key. Set ``GGBUILD_IGNORE_DIRTY_STATE=1`` to omit only the
dirty-tree component while retaining the version and Git revision.

Cache behavior can be overridden for individual bundle nodes. The option is
repeatable and each value can contain comma-separated entries::

    ggbuild ci run \
        --node-cache=bundle-a:ignore,bundle-b:reuse \
        --node-cache=bundle-c:ignore

``ignore`` bypasses an exact cache hit and rebuilds the named node. ``reuse``
first tries the exact key, then reuses the newest structurally compatible
bundle for the same node identity despite a changed content key; if none is
available, the node is built normally. Reused bundles are fully validated and
republished under the current key before downstream nodes execute.

Generate or verify the static Actions workflow::

    ggbuild ci render-workflow
    ggbuild ci check-workflow

For local builds, Dockerfiles are rendered from target-owned templates and
passed directly to ``docker build`` when ggbuild prepares an image. In GitHub
Actions, the same templates produce content-addressed GNU and musl images in
``ghcr.io/vercel-labs/ggbuild``. Each image is built natively for amd64 and
arm64 and runs the selected closure directly, without nested Docker. Image
builds run when their rendering inputs change or when explicitly dispatched.
The project workflow creates its plan at runtime and executes one root closure
per matrix job.

Finally, recipes that declare ggbuild update policies can be updated or checked
without writing files::

    ggbuild update
    ggbuild update --check

Updateable recipes may override ``discover_releases()`` when one latest
version is not enough, for example to return the latest supported release of
each major series. Recipes with patches also declare ``canonical_repo`` and
may override ``canonical_ref(source_version)``. During an update, ggbuild
unpacks each new registered release into a temporary Git repository and uses
the canonical history to reroll patches.

Patch filenames use ``package__name`` followed by an optional exact version or
half-open range, such as ``package__fix__17.10.patch`` or
``package__fix__17-18.patch``. The existing ``package__fix-17.patch`` major
convention remains supported. When variants overlap, the narrowest matching
variant wins; ambiguous overlaps are rejected.

Use ``ggbuild list`` and ``ggbuild <command> --help`` for the complete command
and option reference.


License
-------

Apache 2.0.
