from __future__ import annotations

from dataclasses import dataclass, field

from expenses_tracker.models import Bucket


@dataclass
class BucketNode:
    bucket: Bucket
    children: list["BucketNode"] = field(default_factory=list)


@dataclass
class BucketSelectOption:
    id: int
    label: str
    depth: int


def format_bucket_path(name: str, parent_name: str | None = None) -> str:
    if parent_name:
        return f"{parent_name} › {name}"
    return name


def build_bucket_tree(buckets: list[Bucket]) -> list[BucketNode]:
    by_parent: dict[int | None, list[Bucket]] = {}
    for bucket in buckets:
        by_parent.setdefault(bucket.parent_id, []).append(bucket)

    def build(parent_id: int | None) -> list[BucketNode]:
        nodes: list[BucketNode] = []
        for bucket in sorted(by_parent.get(parent_id, []), key=lambda item: item.name.lower()):
            nodes.append(BucketNode(bucket=bucket, children=build(bucket.id)))
        return nodes

    return build(None)


def flatten_bucket_select_options(
    tree: list[BucketNode],
    *,
    assignable_only: bool = False,
) -> list[BucketSelectOption]:
    options: list[BucketSelectOption] = []

    def walk(nodes: list[BucketNode], ancestors: list[str]) -> None:
        for node in nodes:
            path = " › ".join(ancestors + [node.bucket.name])
            if node.children:
                walk(node.children, ancestors + [node.bucket.name])
                if not assignable_only:
                    options.append(
                        BucketSelectOption(
                            id=node.bucket.id,
                            label=path,
                            depth=len(ancestors),
                        )
                    )
            else:
                options.append(
                    BucketSelectOption(
                        id=node.bucket.id,
                        label=path,
                        depth=len(ancestors),
                    )
                )

    walk(tree, [])
    return options


def resolve_suggested_bucket_id(name: str | None, options: list[BucketSelectOption]) -> int | None:
    if not name:
        return None
    matches = [
        option
        for option in options
        if option.label == name or option.label.split(" › ")[-1] == name
    ]
    if len(matches) == 1:
        return matches[0].id
    return matches[0].id if matches else None
