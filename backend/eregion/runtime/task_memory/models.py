from sqlalchemy import Column, String, Integer, Text, ForeignKey, UniqueConstraint
from eregion.api.db import Base

class TaskMemorySourceRow(Base):
    __tablename__ = "task_memory_sources"
    source_id = Column(String(64), primary_key=True)
    owner_id = Column(String(128), nullable=False, index=True)
    application_id = Column(String(128), nullable=False, index=True)
    task_id = Column(String(40), ForeignKey("career_tasks.task_id", ondelete="CASCADE"), nullable=False, index=True)
    source_kind = Column(String(32), nullable=False)
    source_key = Column(String(128), nullable=False)
    task_version = Column(Integer, nullable=False)
    payload_json = Column(Text, nullable=False)

class TaskMemorySummaryRow(Base):
    __tablename__ = "task_memory_summaries"
    __table_args__ = (UniqueConstraint("owner_id", "application_id", name="uq_task_memory_scope"),)
    summary_id = Column(String(64), primary_key=True)
    owner_id = Column(String(128), nullable=False)
    application_id = Column(String(128), nullable=False)
    version = Column(Integer, nullable=False)
    fingerprint = Column(String(64), nullable=False)
    generator_version = Column(String(40), nullable=False)
    payload_json = Column(Text, nullable=False)
