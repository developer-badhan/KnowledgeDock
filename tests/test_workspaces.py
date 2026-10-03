"""Phase 03 — workspaces, membership and isolation.

`SKILL.md` §26 names the two authorization cases that matter most, and they are
the backbone of this file:

    * a user cannot access another workspace
    * a user cannot access another workspace's resources

The second is only testable once documents exist (Phase 4). What is testable now
is the gate they will both go through: `AuthorizeWorkspace`. Every isolation test
here asserts that the gate refuses, and that the refusal is indistinguishable
from "no such workspace".
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from knowledgedock.domain.workspaces import WorkspaceRole
from tests.conftest import Actor, assert_hidden_from_outsider, make_actor, workspace_of

PASSWORD = "a-perfectly-fine-password"


def asyncio_run(coro):
    """Run a coroutine from a synchronous test body."""
    return asyncio.run(coro)


@pytest.fixture
def actor(new_client) -> callable:
    """Factory: `actor("boss")` returns a signed-in user with its own cookie jar."""

    def _make(label: str) -> Actor:
        return make_actor(new_client(), label)

    return _make


@pytest.fixture
def owner(actor) -> Actor:
    return actor("owner")


class TestCreation:
    def test_creator_becomes_owner(self, owner: Actor) -> None:
        body = owner.client.post("/workspaces", json={"name": "Acme Docs"}).json()

        assert body["role"] == "owner"
        assert body["name"] == "Acme Docs"
        assert body["owner_id"] == owner.user_id
        assert body["member_count"] == 1

    def test_requires_authentication(self, client: TestClient) -> None:
        assert client.post("/workspaces", json={"name": "Acme"}).status_code == 401

    def test_whitespace_is_collapsed(self, owner: Actor) -> None:
        body = owner.client.post("/workspaces", json={"name": "  Acme   Docs  "}).json()

        assert body["name"] == "Acme Docs"

    @pytest.mark.parametrize("name", ["", "   ", "x" * 81])
    def test_invalid_names_are_rejected(self, owner: Actor, name: str) -> None:
        response = owner.client.post("/workspaces", json={"name": name})

        assert response.status_code == 422


class TestListing:
    def test_new_user_has_none(self, owner: Actor) -> None:
        assert owner.client.get("/workspaces").json() == []

    def test_lists_only_my_workspaces(self, actor) -> None:
        mine = actor("mine")
        theirs = actor("theirs")
        my_ws = workspace_of(mine.client, "Mine")
        workspace_of(theirs.client, "Theirs")

        listed = mine.client.get("/workspaces").json()

        assert [w["id"] for w in listed] == [my_ws["id"]]

    def test_reports_role_and_member_count(self, owner: Actor) -> None:
        workspace_of(owner.client, "Acme")

        body = owner.client.get("/workspaces").json()[0]

        assert body["role"] == "owner"
        assert body["member_count"] == 1

    def test_requires_authentication(self, client: TestClient) -> None:
        assert client.get("/workspaces").status_code == 401


class TestWorkspaceIsolation:
    """The core requirement. A non-member must learn nothing."""

    def test_cannot_read_another_workspace(self, actor) -> None:
        mine = actor("mine")
        other = actor("other")
        secret = workspace_of(other.client, "Private Plans")

        response = mine.client.get(f"/workspaces/{secret['id']}")

        assert response.status_code == 404

    def test_non_membership_is_indistinguishable_from_absence(self, actor) -> None:
        """A 403 would confirm the workspace exists, enabling enumeration."""
        mine = actor("mine")
        other = actor("other")
        real = workspace_of(other.client, "Private Plans")
        fabricated = str(uuid.uuid4())

        as_non_member = mine.client.get(f"/workspaces/{real['id']}")
        as_missing = mine.client.get(f"/workspaces/{fabricated}")

        assert as_non_member.status_code == as_missing.status_code == 404
        assert as_non_member.json() == as_missing.json()

    def test_cannot_rename_another_workspace(self, actor) -> None:
        mine = actor("mine")
        other = actor("other")
        theirs = workspace_of(other.client, "Theirs")

        response = mine.client.patch(f"/workspaces/{theirs['id']}", json={"name": "Hijacked"})

        assert response.status_code == 404
        assert other.client.get(f"/workspaces/{theirs['id']}").json()["name"] == "Theirs"

    def test_cannot_delete_another_workspace(self, actor) -> None:
        mine = actor("mine")
        other = actor("other")
        theirs = workspace_of(other.client, "Theirs")

        assert mine.client.delete(f"/workspaces/{theirs['id']}").status_code == 404
        assert other.client.get(f"/workspaces/{theirs['id']}").status_code == 200

    def test_cannot_list_another_workspaces_members(self, actor) -> None:
        mine = actor("mine")
        other = actor("other")
        theirs = workspace_of(other.client, "Theirs")

        assert mine.client.get(f"/workspaces/{theirs['id']}/members").status_code == 404

    def test_cannot_add_themselves_to_another_workspace(self, actor) -> None:
        mine = actor("mine")
        other = actor("other")
        theirs = workspace_of(other.client, "Theirs")

        response = mine.client.post(
            f"/workspaces/{theirs['id']}/members", json={"email": mine.email}
        )

        assert response.status_code == 404
        members = other.client.get(f"/workspaces/{theirs['id']}/members").json()
        assert len(members) == 1

    def test_cannot_remove_a_member_from_another_workspace(self, actor) -> None:
        mine = actor("mine")
        other = actor("other")
        theirs = workspace_of(other.client, "Theirs")

        response = mine.client.delete(f"/workspaces/{theirs['id']}/members/{other.user_id}")

        assert response.status_code == 404

    def test_malformed_workspace_id_is_a_validation_error(self, owner: Actor) -> None:
        assert owner.client.get("/workspaces/not-a-uuid").status_code == 422

    def test_every_workspace_route_requires_authentication(self, client: TestClient) -> None:
        ws = str(uuid.uuid4())

        assert client.get("/workspaces").status_code == 401
        assert client.post("/workspaces", json={"name": "x"}).status_code == 401
        assert client.get(f"/workspaces/{ws}").status_code == 401
        assert client.patch(f"/workspaces/{ws}", json={"name": "x"}).status_code == 401
        assert client.delete(f"/workspaces/{ws}").status_code == 401
        assert client.get(f"/workspaces/{ws}/members").status_code == 401
        assert (
            client.post(f"/workspaces/{ws}/members", json={"email": "a@b.com"}).status_code == 401
        )
        assert client.delete(f"/workspaces/{ws}/members/{ws}").status_code == 401


class TestRoles:
    def test_owner_can_rename(self, owner: Actor) -> None:
        ws = workspace_of(owner.client)

        response = owner.client.patch(f"/workspaces/{ws['id']}", json={"name": "Renamed"})

        assert response.status_code == 200
        assert response.json()["name"] == "Renamed"

    def test_member_cannot_rename(self, actor) -> None:
        owner = actor("boss")
        helper = actor("helper")
        ws = workspace_of(owner.client)
        owner.client.post(f"/workspaces/{ws['id']}/members", json={"email": helper.email})

        response = helper.client.patch(f"/workspaces/{ws['id']}", json={"name": "Nope"})

        # 403, not 404: the member knows this workspace exists, so there is
        # nothing left to hide. Only non-membership is concealed.
        assert response.status_code == 403

    def test_member_cannot_delete(self, actor) -> None:
        owner = actor("boss")
        helper = actor("helper")
        ws = workspace_of(owner.client)
        owner.client.post(f"/workspaces/{ws['id']}/members", json={"email": helper.email})

        assert helper.client.delete(f"/workspaces/{ws['id']}").status_code == 403
        assert owner.client.get(f"/workspaces/{ws['id']}").status_code == 200

    def test_owner_can_delete(self, owner: Actor) -> None:
        ws = workspace_of(owner.client)

        assert owner.client.delete(f"/workspaces/{ws['id']}").status_code == 204
        assert owner.client.get(f"/workspaces/{ws['id']}").status_code == 404

    def test_deleted_workspace_leaves_no_orphaned_membership(self, actor) -> None:
        owner = actor("boss")
        helper = actor("helper")
        ws = workspace_of(owner.client)
        owner.client.post(f"/workspaces/{ws['id']}/members", json={"email": helper.email})

        owner.client.delete(f"/workspaces/{ws['id']}")

        assert helper.client.get("/workspaces").json() == []


class TestMembership:
    def test_owner_is_a_member_from_the_start(self, owner: Actor) -> None:
        ws = workspace_of(owner.client)

        members = owner.client.get(f"/workspaces/{ws['id']}/members").json()

        assert len(members) == 1
        assert members[0]["role"] == "owner"
        assert members[0]["user_id"] == owner.user_id

    def test_owner_can_add_an_existing_user(self, actor) -> None:
        owner = actor("boss")
        helper = actor("helper")
        ws = workspace_of(owner.client)

        response = owner.client.post(
            f"/workspaces/{ws['id']}/members", json={"email": helper.email}
        )

        assert response.status_code == 201
        assert response.json()["role"] == "member"

    def test_added_member_can_then_see_it(self, actor) -> None:
        owner = actor("boss")
        helper = actor("helper")
        ws = workspace_of(owner.client)
        assert helper.client.get(f"/workspaces/{ws['id']}").status_code == 404

        owner.client.post(f"/workspaces/{ws['id']}/members", json={"email": helper.email})

        assert helper.client.get(f"/workspaces/{ws['id']}").status_code == 200
        assert [w["id"] for w in helper.client.get("/workspaces").json()] == [ws["id"]]

    def test_member_email_is_normalised(self, actor) -> None:
        owner = actor("boss")
        helper = actor("helper")
        ws = workspace_of(owner.client)

        response = owner.client.post(
            f"/workspaces/{ws['id']}/members", json={"email": helper.email.upper()}
        )

        assert response.status_code == 201

    def test_adding_twice_conflicts(self, actor) -> None:
        owner = actor("boss")
        helper = actor("helper")
        ws = workspace_of(owner.client)
        owner.client.post(f"/workspaces/{ws['id']}/members", json={"email": helper.email})

        response = owner.client.post(
            f"/workspaces/{ws['id']}/members", json={"email": helper.email}
        )

        assert response.status_code == 409

    def test_unknown_email_is_not_found(self, owner: Actor) -> None:
        ws = workspace_of(owner.client)

        response = owner.client.post(
            f"/workspaces/{ws['id']}/members", json={"email": "ghost@example.com"}
        )

        assert response.status_code == 404

    def test_member_cannot_add_someone(self, actor) -> None:
        owner = actor("boss")
        helper = actor("helper")
        third = actor("third")
        ws = workspace_of(owner.client)
        owner.client.post(f"/workspaces/{ws['id']}/members", json={"email": helper.email})

        response = helper.client.post(
            f"/workspaces/{ws['id']}/members", json={"email": third.email}
        )

        assert response.status_code == 403

    def test_owner_cannot_add_themselves(self, owner: Actor) -> None:
        ws = workspace_of(owner.client)

        response = owner.client.post(f"/workspaces/{ws['id']}/members", json={"email": owner.email})

        assert response.status_code == 422

    def test_member_can_leave(self, actor) -> None:
        owner = actor("boss")
        helper = actor("helper")
        ws = workspace_of(owner.client)
        owner.client.post(f"/workspaces/{ws['id']}/members", json={"email": helper.email})

        response = helper.client.delete(f"/workspaces/{ws['id']}/members/{helper.user_id}")

        assert response.status_code == 204
        assert helper.client.get(f"/workspaces/{ws['id']}").status_code == 404

    def test_owner_cannot_be_removed(self, actor) -> None:
        """Removing the owner would orphan every document in the workspace."""
        owner = actor("boss")
        ws = workspace_of(owner.client)

        response = owner.client.delete(f"/workspaces/{ws['id']}/members/{owner.user_id}")

        assert response.status_code == 409
        assert owner.client.get(f"/workspaces/{ws['id']}").status_code == 200

    def test_member_cannot_remove_another_member(self, actor) -> None:
        owner = actor("boss")
        helper = actor("helper")
        third = actor("third")
        ws = workspace_of(owner.client)
        for person in (helper, third):
            owner.client.post(f"/workspaces/{ws['id']}/members", json={"email": person.email})

        response = helper.client.delete(f"/workspaces/{ws['id']}/members/{third.user_id}")

        assert response.status_code == 403
        assert len(owner.client.get(f"/workspaces/{ws['id']}/members").json()) == 3

    def test_owner_can_remove_a_member(self, actor) -> None:
        owner = actor("boss")
        helper = actor("helper")
        ws = workspace_of(owner.client)
        owner.client.post(f"/workspaces/{ws['id']}/members", json={"email": helper.email})

        response = owner.client.delete(f"/workspaces/{ws['id']}/members/{helper.user_id}")

        assert response.status_code == 204
        assert len(owner.client.get(f"/workspaces/{ws['id']}/members").json()) == 1

    def test_removed_member_loses_access_immediately(self, actor) -> None:
        owner = actor("boss")
        helper = actor("helper")
        ws = workspace_of(owner.client)
        owner.client.post(f"/workspaces/{ws['id']}/members", json={"email": helper.email})
        assert helper.client.get(f"/workspaces/{ws['id']}").status_code == 200

        owner.client.delete(f"/workspaces/{ws['id']}/members/{helper.user_id}")

        assert helper.client.get(f"/workspaces/{ws['id']}").status_code == 404

    def test_removed_members_existing_session_stops_working(self, actor) -> None:
        """The session is still cryptographically valid; only membership changed.

        Proves the isolation check runs per request rather than trusting whatever
        was true when the token was issued.
        """
        owner = actor("boss")
        helper = actor("helper")
        ws = workspace_of(owner.client)
        owner.client.post(f"/workspaces/{ws['id']}/members", json={"email": helper.email})

        owner.client.delete(f"/workspaces/{ws['id']}/members/{helper.user_id}")

        assert helper.client.get("/auth/me").status_code == 200  # still signed in
        assert helper.client.get(f"/workspaces/{ws['id']}").status_code == 404

    def test_cannot_remove_someone_who_is_not_a_member(self, owner: Actor, actor) -> None:
        ws = workspace_of(owner.client)
        stranger = actor("stranger")

        response = owner.client.delete(f"/workspaces/{ws['id']}/members/{stranger.user_id}")

        assert response.status_code == 404

    def test_member_list_puts_owner_first(self, actor) -> None:
        owner = actor("boss")
        helper = actor("helper")
        ws = workspace_of(owner.client)
        owner.client.post(f"/workspaces/{ws['id']}/members", json={"email": helper.email})

        members = owner.client.get(f"/workspaces/{ws['id']}/members").json()

        assert members[0]["role"] == WorkspaceRole.OWNER


class TestWorkspaceRoleModel:
    def test_owner_capabilities(self) -> None:
        assert WorkspaceRole.OWNER.can_manage_members
        assert WorkspaceRole.OWNER.can_modify_workspace
        assert WorkspaceRole.OWNER.can_delete_workspace

    def test_member_capabilities(self) -> None:
        assert not WorkspaceRole.MEMBER.can_manage_members
        assert not WorkspaceRole.MEMBER.can_modify_workspace
        assert not WorkspaceRole.MEMBER.can_delete_workspace


class TestWorkspaceOutage:
    """A broken deployment must not look like a brand new account."""

    def test_missing_repository_is_503(self, settings) -> None:
        from fastapi.testclient import TestClient as TC

        from knowledgedock.app import create_app
        from knowledgedock.infrastructure.repositories.workspace_repository import (
            UnavailableWorkspaceRepository,
        )
        from tests.conftest import FakeMongoManager, InMemoryUserRepository

        app = create_app(
            settings,
            mongo_manager=FakeMongoManager(reachable=False),
            user_repository=InMemoryUserRepository(),
            workspace_repository=UnavailableWorkspaceRepository(RuntimeError("no Atlas")),
        )
        with TC(app) as client:
            client.post("/auth/register", json={"email": "a@example.com", "password": PASSWORD})
            client.post("/auth/login", json={"email": "a@example.com", "password": PASSWORD})

            response = client.get("/workspaces")

            assert response.status_code == 503
            assert response.json()["error"]["code"] == "service_unavailable"

    def test_query_timeout_after_login_is_503_not_500(self, settings) -> None:
        """The failure mode that actually happens: Atlas dies mid-session."""
        from fastapi.testclient import TestClient as TC
        from pymongo.errors import ServerSelectionTimeoutError

        from knowledgedock.app import create_app
        from tests.conftest import FakeMongoManager, InMemoryUserRepository

        class DeadWorkspaceRepository:
            """Users are reachable; only the workspace collections are not."""

            async def ensure_indexes(self) -> None:
                return None

            async def list_for_user(self, user_id):
                raise ServerSelectionTimeoutError("no Atlas reachable")

            async def create(self, *a, **k):
                raise ServerSelectionTimeoutError("no Atlas reachable")

            async def find_by_id(self, workspace_id):
                raise ServerSelectionTimeoutError("no Atlas reachable")

            async def find_member(self, workspace_id, user_id):
                raise ServerSelectionTimeoutError("no Atlas reachable")

            async def list_members(self, workspace_id):
                raise ServerSelectionTimeoutError("no Atlas reachable")

        app = create_app(
            settings,
            mongo_manager=FakeMongoManager(reachable=False),
            user_repository=InMemoryUserRepository(),
            workspace_repository=DeadWorkspaceRepository(),
        )
        with TC(app) as client:
            client.post("/auth/register", json={"email": "b@example.com", "password": PASSWORD})
            client.post("/auth/login", json={"email": "b@example.com", "password": PASSWORD})

            response = client.get("/workspaces")

            assert response.status_code == 503
            assert response.json()["error"]["code"] == "service_unavailable"


class TestMongoRepositorySemantics:
    """Guards on behaviour the in-memory double cannot reproduce.

    `InMemoryWorkspaceRepository` is hand-written, so it agrees with whatever the
    use cases expect. These tests pin the PyMongo-level details that are easy to
    get wrong and invisible until a real cluster is involved.
    """

    def test_rename_returns_the_updated_document(self) -> None:
        """PyMongo defaults to BEFORE; a rename must reply with the NEW name.

        Without `return_document=ReturnDocument.AFTER` the route returns the
        pre-update document, so `PATCH /workspaces/{id}` echoes the old name and
        the client sees the rename as a no-op. Found by running against Atlas.
        """
        import uuid

        from knowledgedock.infrastructure.repositories.workspace_repository import (
            MongoWorkspaceRepository,
        )

        workspace_id = uuid.uuid4()
        stored = {
            "_id": workspace_id,
            "name": "before",
            "owner_id": uuid.uuid4(),
            "created_at": "t0",
            "updated_at": "t0",
        }

        class HonouringCollection:
            """Emulates PyMongo's BEFORE default unless told otherwise."""

            def __init__(self) -> None:
                self.kwargs: dict = {}

            async def find_one_and_update(self, *args, **kwargs):
                from pymongo import ReturnDocument

                self.kwargs = kwargs
                if kwargs.get("return_document") is ReturnDocument.AFTER:
                    stored["name"] = args[1]["$set"]["name"]
                return dict(stored)

        class DB:
            def __getitem__(self, name):
                return collection

        collection = HonouringCollection()
        repo = MongoWorkspaceRepository(DB())
        updated = asyncio_run(repo.rename(workspace_id, "after", "t1"))

        assert updated is not None
        assert updated.name == "after"

    def test_ensure_indexes_creates_the_expected_constraints(self) -> None:

        from knowledgedock.infrastructure.repositories.workspace_repository import (
            MongoWorkspaceRepository,
        )

        calls: list[tuple] = []

        class RecordingCollection:
            async def create_index(self, keys, **kwargs):
                calls.append((keys, kwargs))
                return "ok"

        class DB:
            def __getitem__(self, name):
                return RecordingCollection()

        asyncio_run(MongoWorkspaceRepository(DB()).ensure_indexes())

        unique_on_members = [c for c in calls if len(c[0]) == 2 and c[1].get("unique")]
        assert unique_on_members, "membership uniqueness must be enforced by an index"


class TestRefusalIsOpaque:
    """The house rule, stated once and applied to every workspace route.

    KnowledgeDock has one access boundary: the workspace. Everyone inside it can
    see everything, so there is a single refusal for a resource you cannot reach,
    and it must be indistinguishable from the resource never having existed.
    Phase 4's document routes inherit this by depending on
    `require_workspace_access` and by querying on `(id, workspace_id)` together.
    """

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("GET", "/workspaces/{ws}", None),
            ("PATCH", "/workspaces/{ws}", {"name": "Renamed"}),
            ("DELETE", "/workspaces/{ws}", None),
            ("GET", "/workspaces/{ws}/members", None),
            ("POST", "/workspaces/{ws}/members", {"email": "someone@example.com"}),
            ("DELETE", "/workspaces/{ws}/members/{user}", None),
        ],
    )
    def test_outsider_and_absent_id_are_indistinguishable(
        self, actor, method: str, path: str, body
    ) -> None:
        owner = actor("boss")
        outsider = actor("stranger")
        secret = workspace_of(owner.client, "Private Plans")

        def call(target: str):
            request = getattr(outsider.client, method.lower())
            return (
                request(path.format(ws=target, user=owner.user_id), json=body)
                if body is not None
                else request(path.format(ws=target, user=owner.user_id))
            )

        real = call(secret["id"])
        missing = call(str(uuid.uuid4()))

        assert_hidden_from_outsider(real, missing, f"{method} {path}")

    @pytest.mark.parametrize(
        "path",
        [
            "/workspaces/{ws}",
            "/workspaces/{ws}/members",
        ],
    )
    def test_member_also_gets_404_for_another_workspaces_resources(self, actor, path: str) -> None:
        """Two members of *different* workspaces are still outsiders to each other."""
        a = actor("a")
        b = actor("b")
        shared_a = workspace_of(a.client, "A's space")
        shared_b = workspace_of(b.client, "B's space")

        # Each is a member somewhere, which changes nothing for the other's space.
        for actor_ref, own, foreign in ((a, shared_a, shared_b), (b, shared_b, shared_a)):
            assert actor_ref.client.get(path.format(ws=foreign["id"])).status_code == 404, (
                "membership elsewhere must not grant access here"
            )
            assert actor_ref.client.get(path.format(ws=own["id"])).status_code == 200
