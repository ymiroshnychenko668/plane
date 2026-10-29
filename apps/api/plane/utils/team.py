"""Validation and read-only schema-1 compatibility for the Teams registry."""

import hashlib
import html
import json
import re
from functools import wraps
from uuid import UUID

from django.db import transaction

from rest_framework.exceptions import APIException, ValidationError

from plane.db.models import Page, Project, ProjectMember, ProjectTeamContact, ProjectTeamMigration, WorkspaceMember

KIND = "agent-platform-hcom-team"
ROLES_KIND = "agent-platform-chat-roles"
BLOCK = re.compile(r"<pre\b[^>]*>\s*<code\b[^>]*>(.*?)</code>\s*</pre>", re.S | re.I)


class TeamConflict(APIException):
    status_code = 409
    default_detail = "The team data conflicts with its existing source."


def address_value(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise ValidationError(
            {"address": "Use a logical address of up to 128 letters, digits, underscores or hyphens."}
        )
    return value


def target_value(runtime, target):
    allowed = {"codex": {"thread_id", "host_id"}, "hermes": {"agent_id", "conversation_id", "profile", "host_id"}}
    required = {"codex": "thread_id", "hermes": "agent_id"}
    if (
        runtime not in allowed
        or not isinstance(target, dict)
        or set(target) - allowed[runtime]
        or required[runtime] not in target
    ):
        raise ValidationError({"target": "Expected the registered runtime's receiver fields."})
    for key, value in target.items():
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 255
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@+-]*", value)
        ):
            raise ValidationError({"target": "Receiver identifiers cannot contain whitespace, URLs or credentials."})
        if key in {"thread_id", "conversation_id"}:
            try:
                UUID(value)
            except ValueError:
                raise ValidationError({"target": "Thread and conversation IDs must be UUIDs."}) from None
    return target


def target_matches(registration, target):
    binding = registration.binding or {}
    runtime = registration.runtime
    try:
        target_value(runtime, target)
    except ValidationError:
        return False
    if runtime == "codex":
        if binding.get("thread_id") != target.get("thread_id"):
            return False
        if registration.agent_key.startswith("chat-"):
            if registration.agent_key != "chat-" + UUID(target["thread_id"]).hex:
                return False
            if not registration.display_name or not registration.provisioning.get("role"):
                return False
        return not binding.get("host_id") or binding["host_id"] == target.get("host_id")
    if binding.get("agent_id") != target.get("agent_id"):
        return False
    for key in ("host_id", "profile"):
        if key in binding and binding[key] != target.get(key):
            return False
    return binding.get("session_id") == target.get("conversation_id")


def member_active(project, user_id):
    return bool(
        user_id
        and ProjectMember.objects.filter(
            project=project, member_id=user_id, is_active=True, member__is_active=True
        ).exists()
        and WorkspaceMember.objects.filter(
            workspace_id=project.workspace_id, member_id=user_id, is_active=True
        ).exists()
    )


def active_member_ids(project):
    return set(
        ProjectMember.objects.filter(
            project=project,
            is_active=True,
            member__is_active=True,
            member__member_workspace__workspace_id=project.workspace_id,
            member__member_workspace__is_active=True,
        ).values_list("member_id", flat=True)
    )


def available(contact, active_users=None):
    actor = contact.registration
    return bool(
        contact.active
        and actor.active
        and actor.project_id == contact.project_id
        and (
            actor.user_id in active_users if active_users is not None else member_active(contact.project, actor.user_id)
        )
        and target_matches(actor, contact.target)
    )


def legacy_pages(project):
    return Page.objects.filter(
        project_pages__project=project,
        project_pages__deleted_at__isnull=True,
        workspace_id=project.workspace_id,
        name="hcom team",
        archived_at__isnull=True,
    ).distinct()


def legacy_status(project):
    receipt = ProjectTeamMigration.objects.filter(project=project).first()
    if receipt:
        return {"page_id": str(receipt.page_id), "status": "imported"}
    pages = list(legacy_pages(project).values_list("id", flat=True)[:2])
    return {
        "page_id": str(pages[0]) if len(pages) == 1 else None,
        "status": "conflict" if len(pages) > 1 else "pending" if pages else "none",
    }


def require_migrated(project):
    if legacy_status(project)["status"] in {"pending", "conflict"}:
        raise TeamConflict("Import the existing hcom team page before changing the Teams address registry.")


def parse_document(content, project):
    found = []
    for match in BLOCK.finditer(content):
        raw = html.unescape(match.group(1))
        try:
            document = json.loads(raw)
        except ValueError:
            if KIND in raw:
                raise TeamConflict("The legacy team block is malformed; its source has been preserved.") from None
            continue
        if isinstance(document, dict) and document.get("kind") == KIND:
            found.append((document, match.span()))
    if len(found) != 1:
        raise TeamConflict("Expected exactly one structured hcom team block.")
    document, span = found[0]
    if (
        document.get("schema_version") != 1
        or document.get("project_id") != str(project.id)
        or document.get("workspace_slug") != project.workspace.slug
        or not isinstance(document.get("contacts"), dict)
    ):
        raise TeamConflict("The legacy team block has a different project or schema.")
    if len(document["contacts"]) > 10000:
        raise ValidationError("The legacy contact limit is 10000.")
    required = {"runtime", "agent_key", "user_id", "registration_id", "address", "target"}
    for address, contact in document["contacts"].items():
        address_value(address)
        if (
            not isinstance(contact, dict)
            or not required <= set(contact)
            or set(contact) - required - {"display_name", "role", "chat_url"}
        ):
            raise TeamConflict("Legacy contact fields are invalid.")
        if contact["address"] != address:
            raise TeamConflict("Legacy address and key differ.")
        target_value(contact["runtime"], contact["target"])
        for key in ("registration_id", "user_id"):
            try:
                UUID(contact[key])
            except (ValueError, TypeError, AttributeError):
                raise TeamConflict("Legacy identity IDs must be UUIDs.") from None
    return document, span


def assert_legacy_page_writable(page_id):
    if ProjectTeamMigration.objects.filter(page_id=page_id).exists():
        raise TeamConflict(
            "This address page was imported into Teams. Manage its contacts through Teams; "
            "the preserved page is read-only."
        )


def lock_legacy_page_name(project_id, name):
    """Call inside the persistence transaction, before taking page row locks."""
    if name == "hcom team":
        Project.objects.select_for_update().get(id=project_id)
        if ProjectTeamMigration.objects.filter(project_id=project_id).exists():
            raise TeamConflict("This project's address book is managed in Teams; do not create another hcom team page.")


def locked_legacy_page_mutation(method):
    """Serialize persisted page writes with import, including ancestor mutations.

    Decorate SQL mutations, never the outer API-to-live proxy (its callback must
    be able to acquire its own lock).
    """

    @wraps(method)
    def wrapped(self, request, slug, project_id, page_id, *args, **kwargs):
        with transaction.atomic():
            if method.__name__ == "partial_update":
                lock_legacy_page_name(project_id, request.data.get("name"))
            page_ids = {page_id}
            if method.__name__ in {"archive", "unarchive", "destroy"}:
                frontier = {page_id}
                while frontier:
                    children = set(Page.objects.filter(parent_id__in=frontier).values_list("id", flat=True))
                    frontier = children - page_ids
                    page_ids.update(frontier)
            # Hold every affected row through the original mutation. This also
            # prevents importing a child while an ancestor archive is in flight.
            list(Page.objects.select_for_update().filter(id__in=page_ids).order_by("id"))
            if ProjectTeamMigration.objects.filter(page_id__in=page_ids).exists():
                raise TeamConflict(
                    "This page or a descendant preserves an imported Teams address source and is read-only."
                )
            return method(self, request, slug, project_id, page_id, *args, **kwargs)

    return wrapped


def legacy_projection(page, project_id):
    """Old hcom clients read the same canonical addresses without a second writer."""
    receipt = (
        ProjectTeamMigration.objects.filter(page_id=page.id, project_id=project_id)
        .select_related("project__workspace")
        .first()
    )
    if not receipt:
        return None
    project = receipt.project
    document, span = parse_document(page.description_html, project)
    document["contacts"] = {}
    roles = {}
    active_users = active_member_ids(project)
    for contact in ProjectTeamContact.objects.filter(project=project).select_related("registration", "project"):
        if not available(contact, active_users):
            continue
        actor = contact.registration
        document["contacts"][contact.address] = {
            "address": contact.address,
            "runtime": actor.runtime,
            "agent_key": actor.agent_key,
            "user_id": str(actor.user_id),
            "registration_id": str(actor.id),
            "target": contact.target,
        }
        if actor.runtime == "codex" and actor.agent_key.startswith("chat-"):
            roles[str(actor.id)] = {
                "display_name": actor.display_name,
                "role": actor.provisioning.get("role", ""),
                "chat_url": "codex://threads/" + actor.binding["thread_id"],
            }

    def block(value):
        return "<pre><code>" + html.escape(json.dumps(value, ensure_ascii=False, sort_keys=True)) + "</code></pre>"

    content = page.description_html[: span[0]] + block(document) + page.description_html[span[1] :]
    # The old derived role view is not authoritative; replace it along with contacts.
    for match in list(BLOCK.finditer(content))[::-1]:
        try:
            value = json.loads(html.unescape(match.group(1)))
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("kind") == ROLES_KIND:
            content = content[: match.start()] + content[match.end() :]
    content += block({"kind": ROLES_KIND, "schema_version": 1, "roles": roles})
    revision = hashlib.sha256(content.encode()).hexdigest()
    return {
        "id": str(page.id),
        "name": page.name,
        "description_html": content,
        "updated_at": page.updated_at.isoformat(),
        "source": "teams",
        "revision": revision,
    }
