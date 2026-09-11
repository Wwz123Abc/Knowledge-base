from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.knowledge_bases import KnowledgeBaseService
from app.models import DocumentAccessGroup, DocumentKnowledgeBase, KnowledgeDocument


def test_knowledge_base_router_uses_keywords_and_can_cross_search():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    service = KnowledgeBaseService()
    with Session(engine) as db:
        hr = service.create(db, "tenant-a", "admin", "人力资源", "员工制度", ["年假", "招聘"])
        finance = service.create(db, "tenant-a", "admin", "财务", "报销制度", ["发票", "差旅"])

        routed = service.route(db, "tenant-a", "差旅发票如何报销")
        cross_search = service.route(db, "tenant-a", "公司有什么规定")

    assert routed[0] == finance.id
    assert hr.id in cross_search and finance.id in cross_search


def test_knowledge_base_can_be_disabled_and_only_unassigned_bases_deleted(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    service = KnowledgeBaseService()

    with Session(engine) as db:
        base = service.create(db, "tenant-a", "admin", "财务", "初始说明", ["报销"])
        updated = service.update(db, base.id, "tenant-a", None, None, None, False)
        assert updated.enabled is False
        updated = service.update(
            db,
            base.id,
            "tenant-a",
            None,
            None,
            None,
            None,
            {"description"},
        )
        assert updated.description is None
        assert service.list(db, "tenant-a") == []
        assert service.list(db, "tenant-a", include_disabled=True)[0].id == base.id

        document = KnowledgeDocument(
            tenant_id="tenant-a",
            title="财务制度",
            filename="finance.md",
            stored_path=str(tmp_path / "finance.md"),
            content_hash="a" * 64,
            status="ready",
        )
        db.add(document)
        db.flush()
        db.add(DocumentKnowledgeBase(document_id=document.id, knowledge_base_id=base.id))
        db.commit()

        try:
            service.delete(db, base.id, "tenant-a")
        except ValueError as exc:
            assert "关联文档" in str(exc)
        else:
            raise AssertionError("assigned knowledge base must not be deleted")

        db.query(DocumentKnowledgeBase).delete()
        db.commit()
        assert service.delete(db, base.id, "tenant-a") is True


def test_list_hides_knowledge_bases_with_no_readable_documents(tmp_path):
    # Regression test: an employee outside a department's access group should not see that
    # department's knowledge base in the chat scope selector at all — otherwise picking it
    # would just silently return zero results, and worse, its existence would leak.
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    service = KnowledgeBaseService()

    with Session(engine) as db:
        open_base = service.create(db, "tenant-a", "admin", "公开手册", None, [])
        restricted_base = service.create(db, "tenant-a", "admin", "部门2机密", None, [])

        open_document = KnowledgeDocument(
            tenant_id="tenant-a",
            title="员工手册",
            filename="handbook.md",
            stored_path=str(tmp_path / "handbook.md"),
            content_hash="a" * 64,
            status="ready",
        )
        restricted_document = KnowledgeDocument(
            tenant_id="tenant-a",
            title="部门2内部文件",
            filename="dept2.md",
            stored_path=str(tmp_path / "dept2.md"),
            content_hash="b" * 64,
            status="ready",
        )
        db.add_all([open_document, restricted_document])
        db.flush()
        db.add(DocumentKnowledgeBase(document_id=open_document.id, knowledge_base_id=open_base.id))
        db.add(
            DocumentKnowledgeBase(
                document_id=restricted_document.id, knowledge_base_id=restricted_base.id
            )
        )
        db.add(DocumentAccessGroup(document_id=restricted_document.id, group_name="dept_2"))
        db.commit()

        outsider_view = service.list(db, "tenant-a", visible_to_groups=["dept_3"])
        assert [base.id for base in outsider_view] == [open_base.id]

        member_view = service.list(db, "tenant-a", visible_to_groups=["dept_2"])
        member_ids = {base.id for base in member_view}
        assert member_ids == {open_base.id, restricted_base.id}

        admin_view = service.list(db, "tenant-a", visible_to_groups=None)
        assert {base.id for base in admin_view} == {open_base.id, restricted_base.id}
