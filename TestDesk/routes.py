"""
HTTP surface for TestDesk. Mounted by interface/api.py:

    from TestDesk.routes import build_router
    app.include_router(build_router(get_current_user, _run_job_sync))

It is a factory (rather than importing from interface.api) so there is no
circular import: the API hands over its auth dependency and its normal
job-run function.

    GET    /testdesk/targets                  Jobs and Agents a scenario can aim at
    GET    /testdesk/scenarios                saved scenarios (yours; admins see all)
    POST   /testdesk/scenarios                save one
    PUT    /testdesk/scenarios/{id}
    DELETE /testdesk/scenarios/{id}
    POST   /testdesk/run                      {"mode": "test"|"prod", "scenario_id": ... | "scenario": {...}}
                                              -> {"result_id"}; runs in the background
    GET    /testdesk/results                  history (summary rows)
    GET    /testdesk/results/{id}             full report; poll until state == "finished"

Auth: any signed-in user. Test mode is sandboxed; prod mode is exactly as
powerful as starting that Job from the Jobs page. Scenarios and results are
visible to their owner and to admins.
"""

from __future__ import annotations

from typing import Callable, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import service, store


class ScenarioIn(BaseModel):
    name: str = ""
    target_type: str = "job"
    target_id: str = ""
    task: str = ""
    expectations: list[str] = Field(default_factory=list)
    expect_status: str = "done"
    job_yaml: str = ""
    options: dict = Field(default_factory=dict)


class RunIn(BaseModel):
    mode: Literal["test", "prod"] = "test"
    scenario_id: Optional[str] = None
    scenario: Optional[ScenarioIn] = None


def build_router(
    get_current_user: Callable,
    run_job_sync: Callable[..., dict],
    known_targets: Callable[[], tuple[set[str], set[str]]] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/testdesk", tags=["testdesk"])

    store.mark_orphaned_results_as_error()  # a result 'running' at startup has no thread behind it

    def known() -> tuple[set[str], set[str]]:
        if known_targets:  # injected by tests; the app uses the real registries
            return known_targets()
        from core.agent_loader import list_agents
        from core.job_loader import list_job_ids

        return set(list_job_ids()), set(list_agents())

    def clean(body: ScenarioIn) -> dict:
        jobs, agents = known()
        try:
            data = service.normalize_scenario(body.model_dump(), known_jobs=jobs, known_agents=agents)
            if data["job_yaml"]:  # full validation against the real agent list, same parser the Jobs page uses
                from core.job_loader import JobLoadError, parse_job_yaml

                try:
                    parse_job_yaml(data["job_yaml"], data["target_id"], source="<scenario draft>")
                except JobLoadError as e:
                    raise service.ScenarioError(str(e)) from e
        except service.ScenarioError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return data

    def can_see(row: dict, user) -> bool:
        return user.role == "admin" or row.get("owner_id") == user.id

    def own_scenario(scenario_id: str, user) -> dict:
        row = store.get_scenario(scenario_id)
        if not row or not can_see(row, user):
            raise HTTPException(status_code=404, detail="unknown scenario")
        return row

    @router.get("/targets")
    async def targets(_=Depends(get_current_user)):
        jobs, agents = known()
        return {"jobs": sorted(jobs), "agents": sorted(agents)}

    @router.get("/scenarios")
    async def list_scenarios(user=Depends(get_current_user)):
        return {"scenarios": store.list_scenarios(None if user.role == "admin" else user.id)}

    @router.post("/scenarios")
    async def create_scenario(body: ScenarioIn, user=Depends(get_current_user)):
        data = clean(body)
        sid = store.create_scenario(**data, owner_id=user.id)
        return store.get_scenario(sid)

    @router.put("/scenarios/{scenario_id}")
    async def update_scenario(scenario_id: str, body: ScenarioIn, user=Depends(get_current_user)):
        own_scenario(scenario_id, user)
        store.update_scenario(scenario_id, **clean(body))
        return store.get_scenario(scenario_id)

    @router.delete("/scenarios/{scenario_id}")
    async def delete_scenario(scenario_id: str, user=Depends(get_current_user)):
        own_scenario(scenario_id, user)
        store.delete_scenario(scenario_id)
        return {"deleted": scenario_id}

    @router.post("/run")
    async def run(body: RunIn, user=Depends(get_current_user)):
        if body.scenario_id:
            row = own_scenario(body.scenario_id, user)
            scenario = {k: row[k] for k in ("id", "name", "target_type", "target_id", "task", "expectations", "expect_status", "job_yaml", "options")}
        elif body.scenario:
            scenario = {**clean(body.scenario), "id": None}
        else:
            raise HTTPException(status_code=422, detail="send a scenario_id or a scenario")
        try:
            result_id = service.start_run(scenario, body.mode, owner_id=user.id, run_job_sync=run_job_sync)
        except service.ScenarioError as e:
            raise HTTPException(status_code=409 if "already running" in str(e) else 400, detail=str(e))
        return {"result_id": result_id, "mode": body.mode}

    @router.get("/results")
    async def list_results(scenario_id: Optional[str] = None, user=Depends(get_current_user)):
        return {"results": store.list_results(None if user.role == "admin" else user.id, scenario_id)}

    @router.get("/results/{result_id}")
    async def get_result(result_id: str, user=Depends(get_current_user)):
        row = store.get_result(result_id)
        if not row or not can_see(row, user):
            raise HTTPException(status_code=404, detail="unknown result")
        return row

    return router
