"""Fit-analysis integration boundary for saved Job Workspace applications."""

from __future__ import annotations

import logging
import hashlib
import json
import time

from eregion.runtime.workspace.repository import JobWorkspaceRepository
from eregion.runtime.workspace.types import ApplicationStatus
from eregion.runtime.evidence.repository import CareerEvidenceRepository
from eregion.runtime.evidence.types import EvidenceLinkType
from eregion.api.services.fit_analysis_service import FitAnalysisService


logger = logging.getLogger(__name__)


class WorkspaceAnalysisService:
    """Persist analysis artifacts without running resume generation or review."""

    def __init__(
        self,
        workspace: JobWorkspaceRepository,
        fit_service: FitAnalysisService | None = None,
        evidence: CareerEvidenceRepository | None = None,
    ) -> None:
        self._workspace = workspace
        self._fit_service = fit_service or FitAnalysisService()
        self._evidence = evidence

    def cache_signature(self):
        signature = getattr(self._fit_service,'cache_signature',None)
        return signature() if callable(signature) else 'custom-fit-service'

    def uses_jev(self):
        selected=getattr(self._fit_service,'uses_jev',None)
        return bool(selected and selected())

    def reusable(self,application_id,*,resume_text,candidate_facts=None):
        """A saved analysis for exactly these inputs (posting snapshot, resume, facts, scorer) that
        analyze_many would return without scoring again; any other conversation may have saved it."""
        app=self._workspace.get_application(application_id)
        snapshot=self._workspace.current_snapshot(application_id)
        return bool(self._cached(app,snapshot.snapshot_id,resume_text,candidate_facts)[1])

    def _cached(self,current,snapshot_id,resume_text,candidate_facts):
        key=(hashlib.sha256(json.dumps([snapshot_id,resume_text,candidate_facts,self.cache_signature()],
             ensure_ascii=False).encode()).hexdigest() if callable(getattr(self._fit_service,'cache_signature',None)) else None)
        lookup=getattr(self._workspace,'cached_fit_analysis',None)
        cached=lookup(current.application_id,key) if key and callable(lookup) else None
        if cached and self.uses_jev():
            report=next((a.content for a in cached if a.artifact_type.value=='match_report'),{})
            if report.get('algorithm')!='jev-only' or (report.get('valid_until') or 0)<=time.time():cached=None
        if current.status in {ApplicationStatus.SAVED,ApplicationStatus.ANALYZING,ApplicationStatus.ANALYSIS_FAILED}:cached=None
        return key,cached

    async def analyze(
        self,
        application_id: str,
        *,
        snapshot_id: str,
        resume_text: str,
        expected_version: int,
        resume_id: str | None = None,
        candidate_facts=None,
        _result=None,
    ):
        current = self._workspace.require_snapshot_for_application(
            application_id, snapshot_id
        )
        if current.version != expected_version:
            from eregion.runtime.workspace.errors import StaleApplicationError

            raise StaleApplicationError("The application version is stale.")

        # The persisted result belongs to exact inputs and model/prompt/schema
        # configuration, so a new chat or server process need not score it again.
        cache_key,cached=self._cached(current,snapshot_id,resume_text,candidate_facts)
        if cached:
            logger.info('Reused saved fit analysis: application=%s model_calls=0', application_id)
            report=next((a.content for a in cached if a.artifact_type.value=='match_report'),{})
            return current, None, report.get('analysis_status','completed'), cached

        analyzing = self._workspace.transition_status(
            application_id,
            target_status=ApplicationStatus.ANALYZING,
            expected_version=expected_version,
        )
        try:
            snapshot = self._workspace.current_snapshot(application_id)
            result = _result or await self._fit_service.analyze(
                resume_text=resume_text,
                job_description=snapshot.cleaned_job_description,
                **({'candidate_facts':candidate_facts} if self.uses_jev() else {}),
            )
            if self._evidence is not None:
                imported = self._evidence.import_resume_evidence(
                    result.resume_analysis,
                    resume_text,
                    created_by="fit_analysis",
                )
                linked = {
                    item.evidence_id
                    for item in self._evidence.list_for_application(application_id)
                }
                for item in imported:
                    if item.evidence_id in linked:
                        continue
                    self._evidence.link_to_application(
                        item.evidence_id,
                        application_id,
                        expected_version=item.version,
                        link_type=EvidenceLinkType.RELATED,
                        created_by="fit_analysis",
                    )
            state, artifacts = self._workspace.project_fit_analysis(
                application_id,
                snapshot_id=snapshot_id,
                analysis_id=result.analysis_id,
                resume=result.resume_analysis,
                job=result.job_analysis,
                match=result.skill_match,
                expected_version=analyzing.version,
                resume_text=resume_text, resume_id=resume_id,
                analysis_cache_key=cache_key,
                analysis_timing={"model_calls":result.model_calls, "latency_seconds":result.latency_seconds,
                    "stage_seconds":result.stage_seconds, "cache_hits":result.cache_hits,
                    "main_model_calls":result.main_model_calls, "fast_model_calls":result.fast_model_calls,
                    "decision_routing":result.decision_routing},
            )
            has_gaps = bool(
                result.skill_match.missing_required_requirements
                or result.skill_match.missing_preferred_requirements
                or result.skill_match.confirmation_requirements
                or result.skill_match.overall_score is None
                or any(m.match_status=='confirmed_condition' and m.condition_satisfied is False for m in result.skill_match.matches)
            )
            ready = self._workspace.transition_status(
                application_id,
                target_status=(
                    ApplicationStatus.NEEDS_EVIDENCE
                    if has_gaps
                    else ApplicationStatus.MATERIALS_READY
                ),
                expected_version=state.version,
            )
            # Analysis-only executions are not persisted Run workflows.
            return ready, None, result.status, artifacts
        except Exception as exc:
            logger.exception("Workspace fit analysis failed: %s", type(exc).__name__)
            latest = self._workspace.get_application(application_id)
            if latest.status == ApplicationStatus.ANALYZING and latest.version == analyzing.version:
                self._workspace.transition_status(
                    application_id,
                    target_status=ApplicationStatus.ANALYSIS_FAILED,
                    expected_version=latest.version,
                    error_code="fit_analysis_failed",
                )
            raise

    async def analyze_many(self, applications, *, resume_text, candidate_facts=None):
        if not applications or len(applications)>16 or len(set(applications))!=len(applications):
            raise ValueError('Choose one to sixteen unique applications.')
        snapshots={}
        for identifier in applications:
            app=self._workspace.get_application(identifier)
            snapshot=self._workspace.current_snapshot(identifier)
            snapshots[identifier]=(app,snapshot)
        result={}
        jobs=[]
        for identifier,(app,snapshot) in snapshots.items():
            _,cached=self._cached(app,snapshot.snapshot_id,resume_text,candidate_facts)
            if cached:
                report=next((a.content for a in cached if a.artifact_type.value=='match_report'),{})
                result[identifier]=(app,None,report.get('analysis_status','completed'),cached)
            else:
                jobs.append({'id':identifier,'source_version':snapshot.snapshot_id,'job_description':snapshot.cleaned_job_description})
        values=await self._fit_service.analyze_many(resume_text=resume_text,jobs=jobs,candidate_facts=candidate_facts) if jobs else {}
        for identifier,(app,snapshot)in snapshots.items():
            if identifier in result:continue
            from eregion.runtime.provider_usage import current_scope
            guard=current_scope().get('provider_guard')
            if guard:guard()
            result[identifier]=await self.analyze(identifier,snapshot_id=app.current_snapshot_id,
                expected_version=app.version,resume_text=resume_text,candidate_facts=candidate_facts,_result=values[identifier])
        return result
