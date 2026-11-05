import type { ScenarioConfiguration } from "./studio-types";

/** Create the saved scenario on first save, then update that same record. Returns its id. */
export async function saveScenario(scenario: ScenarioConfiguration, savedId?: string, fetchImpl: typeof fetch = fetch): Promise<string> {
  const init = { headers: { "Content-Type": "application/json" }, body: JSON.stringify(scenario) };
  let response = savedId ? await fetchImpl(`/api/scenarios/${encodeURIComponent(savedId)}`, { ...init, method: "PUT" }) : undefined;
  // The saved copy may have been deleted from the Scenarios list; recreate it instead of failing.
  if (!response || response.status === 404) response = await fetchImpl("/api/scenarios", { ...init, method: "POST" });
  if (!response.ok) throw new Error(`Could not save scenario (${response.status}).`);
  return (await response.json() as { id: string }).id;
}
