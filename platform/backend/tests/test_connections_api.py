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

if __name__ == "__main__":
 unittest.main()
