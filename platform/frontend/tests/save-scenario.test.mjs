import assert from 'node:assert/strict';
import test from 'node:test';
import { saveScenario } from '../app/lib/save-scenario.ts';

// In-memory stand-in for /api/scenarios: POST creates, PUT updates or 404s.
function fakeApi() {
  const records = new Map();
  const fetchImpl = async (url, init) => {
    const scenario = JSON.parse(init.body);
    if (init.method === 'POST') {
      const id = `s${records.size + 1}`;
      records.set(id, scenario);
      return Response.json({ id }, { status: 201 });
    }
    const id = decodeURIComponent(url.split('/').at(-1));
    if (!records.has(id)) return Response.json({ detail: 'Scenario not found' }, { status: 404 });
    records.set(id, scenario);
    return Response.json({ id });
  };
  return { records, fetchImpl };
}

test('first save creates, later saves update the same scenario', async () => {
  const { records, fetchImpl } = fakeApi();
  const id = await saveScenario({ title: 'Claims' }, undefined, fetchImpl);
  assert.equal(await saveScenario({ title: 'Claims (edited)' }, id, fetchImpl), id);
  assert.equal(await saveScenario({ title: 'Claims (edited)' }, id, fetchImpl), id);
  assert.equal(records.size, 1);
  assert.equal(records.get(id).title, 'Claims (edited)');
});

test('a deleted saved scenario is recreated', async () => {
  const { records, fetchImpl } = fakeApi();
  const id = await saveScenario({ title: 'Claims' }, 'gone', fetchImpl);
  assert.notEqual(id, 'gone');
  assert.equal(records.size, 1);
});

test('other save failures surface as errors', async () => {
  const fetchImpl = async () => new Response('nope', { status: 500 });
  await assert.rejects(saveScenario({ title: 'Claims' }, 'x', fetchImpl), /Could not save scenario \(500\)/);
});
