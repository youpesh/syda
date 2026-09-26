import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.auth import require_authenticated
from app.database import get_db
from app.models.entities import ScenarioRecord
from app.models.scenario import ScenarioConfiguration
from app.models.user import User

router = APIRouter(prefix="/scenarios", tags=["Scenarios"])


def _apply_configuration(record: ScenarioRecord, scenario: ScenarioConfiguration) -> None:
    configuration = scenario.model_dump(by_alias=True)
    record.title = scenario.title
    record.description = scenario.description
    record.record_count = scenario.record_count
    record.secondary_metric = scenario.secondary_metric.model_dump()
    record.workflow = scenario.workflow
    record.rules = scenario.rules
    record.configuration = configuration


@router.get("")
def list_scenarios(db: Session = Depends(get_db), user: User = Depends(require_authenticated), limit: int = Query(100, ge=1, le=500)):
    records = db.query(ScenarioRecord).filter(ScenarioRecord.user_id == user.id).order_by(ScenarioRecord.updated_at.desc()).limit(limit).all()
    return [record.to_dict() for record in records]


@router.post("", status_code=201)
def create_scenario(scenario: ScenarioConfiguration, db: Session = Depends(get_db), user: User = Depends(require_authenticated)):
    record = ScenarioRecord(id=str(uuid.uuid4()), user_id=user.id)
    _apply_configuration(record, scenario)
    db.add(record)
    db.commit()
    db.refresh(record)
    return record.to_dict()


@router.get("/{scenario_id}")
def get_scenario(scenario_id: str, db: Session = Depends(get_db), user: User = Depends(require_authenticated)):
    record = db.query(ScenarioRecord).filter(ScenarioRecord.id == scenario_id, ScenarioRecord.user_id == user.id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return record.to_dict()


@router.put("/{scenario_id}")
def update_scenario(scenario_id: str, scenario: ScenarioConfiguration, db: Session = Depends(get_db), user: User = Depends(require_authenticated)):
    record = db.query(ScenarioRecord).filter(ScenarioRecord.id == scenario_id, ScenarioRecord.user_id == user.id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Scenario not found")
    _apply_configuration(record, scenario)
    db.commit()
    db.refresh(record)
    return record.to_dict()


@router.delete("/{scenario_id}", status_code=204)
def delete_scenario(scenario_id: str, db: Session = Depends(get_db), user: User = Depends(require_authenticated)):
    record = db.query(ScenarioRecord).filter(ScenarioRecord.id == scenario_id, ScenarioRecord.user_id == user.id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Scenario not found")
    db.delete(record)
    db.commit()
