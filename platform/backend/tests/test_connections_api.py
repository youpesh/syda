import unittest
from unittest.mock import patch
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from fastapi import HTTPException
from app.api import connections as c

class ConnectionTests(unittest.TestCase):
 def request(self):
  return c.ConnectionRequest(dialect='PostgreSQL',host='localhost',port=5432,database='sample',username='user',password='secret-value')
 def test_schema_only_preserves_keys(self):
  engine=create_engine('sqlite://')
  with engine.begin() as db:
   db.exec_driver_sql('CREATE TABLE patients (id INTEGER PRIMARY KEY, name TEXT NOT NULL)')
   db.exec_driver_sql('CREATE TABLE diagnoses (id INTEGER PRIMARY KEY, patient_id INTEGER REFERENCES patients(id))')
   db.exec_driver_sql("INSERT INTO patients VALUES (1, 'private-patient-name')")
  statements=[]
  event.listen(engine,'before_cursor_execute',lambda conn,cursor,statement,params,ctx,many:statements.append(statement))
  with patch.object(c,'connection_engine',return_value=engine): result=c.inspect_connection(self.request())
  self.assertEqual(result['tableCount'],2)
  self.assertTrue(result['schemas']['patients']['id']['primary_key'])
  self.assertEqual(result['schemas']['diagnoses']['patient_id']['references']['schema'],'patients')
  self.assertNotIn('private-patient-name',str(result))
  self.assertFalse(any('SELECT * FROM patients' in s for s in statements))
 def test_driver_error_is_actionable(self):
  with patch.object(c,'connection_engine',side_effect=ModuleNotFoundError('driver')):
   with self.assertRaises(HTTPException) as caught:c.inspect_connection(self.request())
  self.assertEqual(caught.exception.status_code,503)
 def test_credentials_redacted_on_failure_and_engine_disposed(self):
  engine=create_engine('sqlite://')
  with patch.object(c,'connection_engine',return_value=engine),patch.object(c,'inspect',side_effect=OperationalError('secret-value',{},Exception('private'))),patch.object(engine,'dispose') as dispose:
   with self.assertRaises(HTTPException) as caught:c.inspect_connection(self.request())
   dispose.assert_called_once()
  self.assertNotIn('secret-value',str(caught.exception.detail))
 def test_url_escapes_password_and_has_timeout(self):
  with patch.object(c,'create_engine') as create:
   c.connection_engine(self.request())
  self.assertEqual(create.call_args.args[0].password,'secret-value')
  self.assertEqual(create.call_args.kwargs['connect_args']['connect_timeout'],10)

 def test_export_connection_writes_to_database(self):
  import tempfile
  from pathlib import Path
  import pandas as pd
  from unittest.mock import MagicMock
  from app.models.entities import JobRecord

  db = MagicMock()
  user = MagicMock()
  user.id = "user-123"

  job = JobRecord(
   id="job-123",
   user_id=user.id,
   status="complete",
   scenario={"title": "Test"},
  )
  db.query.return_value.filter.return_value.first.return_value = job

  with tempfile.TemporaryDirectory() as tmpdir:
   job_dir = Path(tmpdir) / "job-123"
   job_dir.mkdir(parents=True)
   pd.DataFrame({"id": [1, 2], "name": ["A", "B"]}).to_csv(job_dir / "users.csv", index=False)

   db_file = Path(tmpdir) / "target.db"
   target_engine = create_engine(f"sqlite:///{db_file}")
   with patch.object(c, "DATA_DIR", Path(tmpdir)), patch.object(c, "connection_engine", return_value=target_engine):
    req = c.ConnectionExportRequest(
     dialect="PostgreSQL",
     host="localhost",
     port=5432,
     database="test",
     username="user",
     password="secret-value",
     jobId="job-123",
    )
    res = c.export_connection(req, db=db, user=user)
    self.assertTrue(res["ok"])
    self.assertEqual(res["totalRows"], 2)
    self.assertIn("users", res["tablesExported"])

    # Verify table exists in target database
    written = pd.read_sql("SELECT * FROM users", target_engine)
    self.assertEqual(len(written), 2)

if __name__ == "__main__":
 unittest.main()
