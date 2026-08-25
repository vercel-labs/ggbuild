# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import Any, ClassVar, cast

import json
import os
import pathlib

from cleo.helpers import option
from cleo.io.inputs.option import Option

from ggbuild.ci_protocol import (
    canonical_json,
    digest_json,
    load_plan,
    write_plan,
)
from ggbuild.planner import PlanOptions, create_plan
from ggbuild.project import load_project
from ggbuild.publication import publish_github
from ggbuild.scheduler import RunOptions, format_summary, run_local, run_plan
from ggbuild.targets.linux.dockerfile import (
    bare_test_image,
    bare_test_repository,
    bare_test_source_sha256,
    bare_test_tag,
    docker_environment,
    write_bare_test_context,
)
from ggbuild.workflow import check_generated, write_generated

from . import base


def _sccache_requested(*, default: bool) -> bool:
    value = os.environ.get("GGBUILD_ENABLE_SCCACHE")
    if value is None:
        return default
    return value.lower() in {"1", "on", "true", "yes"}


def _selection_options() -> list[Option]:
    return [
        option(
            "target",
            description="Limit planning to a target triple (repeatable).",
            flag=False,
            multiple=True,
        ),
        option(
            "source-ref",
            description="Limit planning to a registered release (repeatable).",
            flag=False,
            multiple=True,
        ),
    ]


def _matrix(plan: dict[str, Any]) -> list[dict[str, str]]:
    config = load_project()
    roots = plan["roots"]
    if not isinstance(roots, list):
        raise TypeError("plan roots are invalid")
    result = []
    for root in roots:
        if not isinstance(root, dict):
            raise TypeError("plan root is invalid")
        root = cast("dict[str, Any]", root)
        target = str(root["target"])
        version = str(root["version"])
        node = next(
            item for item in plan["nodes"] if item["id"] == root["node_id"]
        )
        result.append({
            "artifact_name": config.workflow.artifact_name.format(
                package=node["package"], target=target, version=version
            ),
            "closure_digest": str(root["closure_digest"]),
            "node_id": str(root["node_id"]),
            "runner": config.target(target).runner,
            "execution": config.target(target).execution,
            "target": target,
            "version": version,
        })
    return result


class CiPlan(base.Command):
    name = "ci plan"
    description = "Create the canonical runtime v3 build plan."
    options: ClassVar = [
        *_selection_options(),
        option(
            "output", description="Write the plan to this path.", flag=False
        ),
        option(
            "github-output",
            description=(
                "Append the runtime root matrix to a GitHub output file."
            ),
            flag=False,
        ),
        option(
            "expected-digest",
            description="Reject a plan that differs from this digest.",
            flag=False,
        ),
    ]

    def handle(self) -> int:
        plan = create_plan(
            options=PlanOptions(
                targets=tuple(self.option("target") or ()),
                versions=tuple(self.option("source-ref") or ()),
            )
        )
        expected_digest = self.option("expected-digest")
        actual_digest = digest_json(plan)
        if expected_digest and expected_digest != actual_digest:
            raise ValueError(
                "generated workflow is stale: expected plan digest "
                f"{expected_digest}, got {actual_digest}; run "
                "ggbuild ci render-workflow"
            )
        output = self.option("output")
        if output:
            write_plan(pathlib.Path(output), plan)
        else:
            self.io.write(canonical_json(plan))
        github_output = self.option("github-output")
        if github_output:
            matrix = json.dumps(
                _matrix(plan),
                sort_keys=True,
                separators=(",", ":"),
            )
            with pathlib.Path(github_output).open(
                "a", encoding="utf-8"
            ) as target:
                target.write(f"matrix={matrix}\n")
        return 0


class CiRun(base.Command):
    name = "ci run"
    description = "Create and execute a project plan locally."
    options: ClassVar = [
        *_selection_options(),
        option("cache-dir", description="Bundle cache directory.", flag=False),
        option(
            "output-dir", description="Artifact output directory.", flag=False
        ),
        option(
            "max-parallel", description="Maximum parallel nodes.", flag=False
        ),
        option("no-cache", description="Rebuild bundle nodes.", flag=True),
        option(
            "node-cache",
            description=(
                "Per-recipe cache policy: "
                "RECIPE_GLOB={auto,ignore,reuse}; accepts shell wildcards "
                "and is repeatable and comma-separated."
            ),
            flag=False,
            multiple=True,
        ),
        option(
            "dry-run", description="Print layers and commands only.", flag=True
        ),
    ]

    def handle(self) -> int:
        config = load_project()
        options = RunOptions(
            targets=tuple(self.option("target") or ()),
            versions=tuple(self.option("source-ref") or ()),
            cache_dir=pathlib.Path(
                self.option("cache-dir") or config.root / ".cache"
            ),
            destination=pathlib.Path(
                self.option("output-dir") or config.root / "dist"
            ),
            max_parallel=int(
                self.option("max-parallel") or config.workflow.max_concurrency
            ),
            no_cache=bool(self.option("no-cache")),
            node_cache=tuple(self.option("node-cache") or ()),
            dry_run=bool(self.option("dry-run")),
            enable_sccache=config.sccache.pull_request,
        )
        summary = run_local(options, config=config)
        self.io.write_line(format_summary(summary, dry_run=options.dry_run))
        return 0


class CiExecuteNode(base.Command):
    name = "ci execute-node"
    description = "Execute one node and its dependency closure."
    options: ClassVar = [
        option("plan", description="Canonical v3 plan path.", flag=False),
        option("node", description="Build plan node identifier.", flag=False),
        option("bundle-dir", description="Bundle cache directory.", flag=False),
        option(
            "install-dir",
            description="Installation cache directory.",
            flag=False,
        ),
        option(
            "output-dir", description="Artifact output directory.", flag=False
        ),
        option("work-dir", description="Node work directory.", flag=False),
        option(
            "max-parallel", description="Maximum parallel nodes.", flag=False
        ),
        option("no-cache", description="Rebuild bundle nodes.", flag=True),
        option(
            "node-cache",
            description=(
                "Per-recipe cache policy: "
                "RECIPE_GLOB={auto,ignore,reuse}; accepts shell wildcards "
                "and is repeatable and comma-separated."
            ),
            flag=False,
            multiple=True,
        ),
        option(
            "prepared-target",
            description=(
                "Execute this Docker target in an already prepared environment."
            ),
            flag=False,
        ),
        option(
            "exact",
            description="Execute only the selected node, not its closure.",
            flag=True,
        ),
        option(
            "bare-linux-test",
            description="Run a Linux test node in its bare runtime image.",
            flag=True,
        ),
        option(
            "dry-run", description="Print layers and commands only.", flag=True
        ),
    ]

    def handle(self) -> int:
        config = load_project()
        plan_path = pathlib.Path(self.option("plan"))
        plan = load_plan(plan_path)
        options = RunOptions(
            cache_dir=pathlib.Path(self.option("bundle-dir")),
            destination=pathlib.Path(self.option("output-dir")),
            install_dir=(
                pathlib.Path(self.option("install-dir"))
                if self.option("install-dir")
                else None
            ),
            work_dir=pathlib.Path(self.option("work-dir")),
            max_parallel=int(
                self.option("max-parallel") or config.workflow.max_concurrency
            ),
            no_cache=bool(self.option("no-cache")),
            node_cache=tuple(self.option("node-cache") or ()),
            dry_run=bool(self.option("dry-run")),
            node_ids=(str(self.option("node")),),
            prepared_target=(
                str(self.option("prepared-target"))
                if self.option("prepared-target")
                else None
            ),
            exact_node=bool(self.option("exact")),
            bare_linux_test=bool(self.option("bare-linux-test")),
            enable_sccache=_sccache_requested(default=False),
        )
        summary = run_plan(plan, options, config=config)
        self.io.write_line(format_summary(summary, dry_run=options.dry_run))
        return 0


class CiRenderActionContext(base.Command):
    name = "ci render-action-context"
    description = "Render a registry-backed Linux Action build context."
    options: ClassVar = [
        option("target", description="Linux target triple.", flag=False),
        option("output", description="Context output directory.", flag=False),
        option(
            "github-output",
            description="Append image metadata to a GitHub output file.",
            flag=False,
        ),
    ]

    def handle(self) -> int:
        target = self.option("target")
        output = self.option("output")
        if not target or not output:
            raise ValueError("--target and --output are required")
        environment = docker_environment(str(target))
        for path in environment.write_action_context(pathlib.Path(output)):
            self.io.write_line(str(path))
        github_output = self.option("github-output")
        if github_output:
            values = {
                "environment": environment.name,
                "image": environment.action_image,
                "platform": environment.platform,
                "repository": environment.action_repository,
                "source_digest": environment.action_source_sha256,
                "tag": environment.action_tag,
            }
            with pathlib.Path(github_output).open(
                "a", encoding="utf-8"
            ) as destination:
                destination.writelines(
                    f"{name}={value}\n" for name, value in values.items()
                )
        return 0


class CiRenderBareTestContext(base.Command):
    name = "ci render-bare-test-context"
    description = "Render a minimal Linux runtime-test image build context."
    options: ClassVar = [
        option(
            "target", description="Linux target triple or libc.", flag=False
        ),
        option("output", description="Context output directory.", flag=False),
        option(
            "github-output",
            description="Append image metadata to a GitHub output file.",
            flag=False,
        ),
    ]

    def handle(self) -> int:
        target = self.option("target")
        output = self.option("output")
        if not target or not output:
            raise ValueError("--target and --output are required")
        target = str(target)
        path = write_bare_test_context(pathlib.Path(output), target)
        self.io.write_line(str(path))
        github_output = self.option("github-output")
        if github_output:
            values = {
                "image": bare_test_image(target),
                "repository": bare_test_repository(target),
                "source_digest": bare_test_source_sha256(target),
                "tag": bare_test_tag(target),
            }
            with pathlib.Path(github_output).open(
                "a", encoding="utf-8"
            ) as destination:
                destination.writelines(
                    f"{name}={value}\n" for name, value in values.items()
                )
        return 0


class CiRenderWorkflow(base.Command):
    name = "ci render-workflow"
    description = "Render the static workflow."

    def handle(self) -> int:
        for path in write_generated():
            self.io.write_line(str(path))
        return 0


class CiCheckWorkflow(base.Command):
    name = "ci check-workflow"
    description = "Reject a stale generated workflow."

    def handle(self) -> int:
        check_generated()
        return 0


class CiPublishGitHub(base.Command):
    name = "ci publish-github"
    description = "Publish every canonical root as a GitHub snapshot."
    options: ClassVar = [
        option("plan", description="Canonical v3 plan path.", flag=False),
        option(
            "artifacts",
            description="Downloaded root artifacts directory.",
            flag=False,
        ),
        option(
            "dry-run", description="Validate and print the snapshot.", flag=True
        ),
        option(
            "github-output",
            description="Append publication state to a GitHub output file.",
            flag=False,
        ),
    ]

    def handle(self) -> int:
        plan_path = self.option("plan")
        artifacts = self.option("artifacts")
        if not plan_path or not artifacts:
            raise ValueError("--plan and --artifacts are required")
        snapshot, tag = publish_github(
            load_plan(pathlib.Path(plan_path)),
            pathlib.Path(artifacts),
            load_project(),
            dry_run=bool(self.option("dry-run")),
        )
        self.io.write(canonical_json(snapshot))
        if output := self.option("github-output"):
            with pathlib.Path(output).open("a", encoding="utf-8") as target:
                published = not self.option("dry-run")
                target.write(f"published={str(published).lower()}\n")
                target.write(f"tag={tag}\n")
        return 0
