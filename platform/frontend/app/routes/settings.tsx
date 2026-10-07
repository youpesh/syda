import { useEffect, useState } from "react";

import type { Route } from "./+types/settings";
import { AppShell } from "~/components/studio/app-shell";
import { Badge } from "~/components/ui/badge";
import { Button } from "~/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "~/components/ui/card";
import { Spinner } from "~/components/ui/spinner";

type Provider = { id: string; name: string; model: string; configured: boolean; source: "user" | null; baseUrl: string | null };
type Detection = { needsProvider: false; provider: string; providerName: string; baseUrl: string | null; models: string[]; recommendedModel: string };
type WorkspaceSettings = {
  preferredProvider: string;
  activeProvider: string;
  activeModel: string;
  providers: Provider[];
};

export function meta({}: Route.MetaArgs) { return [{ title: "Settings — Syda" }]; }

export default function Settings() {
  const [settings, setSettings] = useState<WorkspaceSettings>();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [keyInput, setKeyInput] = useState("");
  const [manualProvider, setManualProvider] = useState("");
  const [baseUrlInput, setBaseUrlInput] = useState("");
  const [needsProvider, setNeedsProvider] = useState(false);
  const [detected, setDetected] = useState<Detection>();
  const [selectedModel, setSelectedModel] = useState("");
  const [detecting, setDetecting] = useState(false);
  const [editingModel, setEditingModel] = useState<string>();
  const [availableModels, setAvailableModels] = useState<string[]>([]);

  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/settings", { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`Could not load settings (${response.status}).`);
        return response.json();
      })
      .then(setSettings)
      .catch((fetchError) => { if (!controller.signal.aborted) setError(fetchError instanceof Error ? fetchError.message : "Settings are unavailable."); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, []);

  async function chooseProvider(provider: string) {
    setSaving(true);
    setError(undefined);
    try {
      const response = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || `Could not save settings (${response.status}).`);
      setSettings(body);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Could not save settings.");
    } finally { setSaving(false); }
  }

  async function detectKey() {
    setDetecting(true);
    setError(undefined);
    try {
      const response = await fetch("/api/settings/detect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: keyInput.trim(), provider: manualProvider || undefined, base_url: manualProvider === "openai_compatible" ? baseUrlInput.trim() : undefined }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Could not identify this key.");
      if (body.needsProvider) {
        setNeedsProvider(true);
        setDetected(undefined);
      } else {
        setDetected(body);
        setSelectedModel(body.recommendedModel);
        setNeedsProvider(false);
      }
    } catch (detectError) {
      setError(detectError instanceof Error ? detectError.message : "Could not identify this key.");
    } finally { setDetecting(false); }
  }

  async function saveKey() {
    if (!detected) return;
    setSaving(true);
    setError(undefined);
    try {
      const response = await fetch("/api/settings/credentials", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: keyInput.trim(), provider: detected.provider, model: selectedModel, base_url: detected.baseUrl }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Could not save this key.");
      setSettings(body);
      setKeyInput("");
      setManualProvider("");
      setBaseUrlInput("");
      setDetected(undefined);
      setSelectedModel("");
      setAddingKey(false);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Could not save this key.");
    } finally { setSaving(false); }
  }

  async function showModels(provider: string) {
    setDetecting(true);
    setError(undefined);
    try {
      const response = await fetch(`/api/settings/models/${provider}`);
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Could not load models.");
      setAvailableModels(body.models);
      setEditingModel(provider);
      const currentModel = settings?.providers.find((item) => item.id === provider)?.model;
      setSelectedModel(currentModel && body.models.includes(currentModel) ? currentModel : body.recommendedModel);
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "Could not load models.");
    } finally { setDetecting(false); }
  }

  async function saveModel() {
    if (!editingModel) return;
    setSaving(true);
    setError(undefined);
    try {
      const response = await fetch(`/api/settings/models/${editingModel}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: selectedModel }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Could not save model.");
      setSettings(body);
      setEditingModel(undefined);
      setAvailableModels([]);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Could not save model.");
    } finally { setSaving(false); }
  }

  async function removeKey(provider: string) {
    setSaving(true);
    setError(undefined);
    try {
      const response = await fetch(`/api/settings/credentials/${provider}`, { method: "DELETE" });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Could not remove this key.");
      setSettings(body);
      setEditingModel(undefined);
    } catch (removeError) {
      setError(removeError instanceof Error ? removeError.message : "Could not remove this key.");
    } finally { setSaving(false); }
  }

  const [addingKey, setAddingKey] = useState(false);
  const activeProvider = settings?.providers.find((provider) => provider.id === settings.activeProvider);
  const activeDescription = settings?.activeProvider === "offline"
    ? "Offline · local schema synthesizer"
    : `${activeProvider?.name ?? "No provider"} · ${settings?.activeModel ?? ""}`;
  const choices = settings ? [
    { id: "automatic", name: "Automatic", detail: "Use the first connected provider", available: true, provider: undefined as Provider | undefined },
    ...settings.providers.map((provider) => ({ id: provider.id, name: provider.name, detail: provider.model || (provider.id === "openai_compatible" ? "Add a key and API URL" : "Choose a model after connecting"), available: provider.configured, provider })),
    { id: "offline", name: "Offline", detail: "Local schema synthesizer · no model API calls", available: true, provider: undefined as Provider | undefined },
  ] : [];

  return <AppShell title="Settings" subtitle="Models and API keys">
    <main className="min-h-0 flex-1 overflow-y-auto px-5 py-6 sm:px-8"><div className="mx-auto max-w-3xl space-y-6">
      <div><h1 className="text-2xl font-semibold tracking-tight">Settings</h1><p className="mt-1 text-sm text-muted-foreground">Choose which model Syda uses to design scenarios and generate data.</p></div>
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      {loading ? <div className="flex items-center gap-2 py-12 text-sm text-muted-foreground"><Spinner /> Loading settings…</div> : settings && <>
        <Card>
          <CardHeader><CardTitle>Model provider</CardTitle><CardDescription>Choose a provider, manage its model, and see its connection status here.</CardDescription></CardHeader>
          <CardContent className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-muted/50 px-3 py-2.5 text-sm"><span className="text-muted-foreground">Used for new requests</span><span className="font-medium">{activeDescription}</span></div>
            <div className="divide-y rounded-lg border" role="radiogroup" aria-label="Preferred provider">
              {choices.map((choice) => {
                const provider = choice.provider;
                const selected = settings.preferredProvider === choice.id;
                return <div className={`flex flex-wrap items-center gap-3 px-3 py-3 ${selected ? "bg-primary/5" : ""}`} key={choice.id}>
                  <button type="button" role="radio" aria-checked={selected} disabled={!choice.available || saving} onClick={() => void chooseProvider(choice.id)} className="flex min-w-0 flex-1 items-center gap-3 text-left disabled:cursor-not-allowed disabled:opacity-60">
                    <span className={`flex size-4 shrink-0 items-center justify-center rounded-full border ${selected ? "border-primary" : "border-muted-foreground/50"}`}>{selected && <span className="size-2 rounded-full bg-primary" />}</span>
                    <span className="min-w-0 flex-1"><span className="flex flex-wrap items-center gap-2 text-sm font-medium">{choice.name}{selected && <Badge variant="secondary">Selected</Badge>}</span><span className="mt-0.5 block truncate text-xs text-muted-foreground">{choice.detail}{provider?.baseUrl ? ` · ${provider.baseUrl}` : ""}</span></span>
                  </button>
                  {provider && <div className="flex shrink-0 items-center gap-1.5">
                    <Badge variant={provider.configured ? "secondary" : "outline"}>{provider.configured ? "Your key" : "Key needed"}</Badge>
                    {provider.configured ? <><Button disabled={detecting || saving} onClick={() => void showModels(provider.id)} size="sm" variant="outline">Model</Button>{provider.source === "user" && <Button disabled={saving} onClick={() => void removeKey(provider.id)} size="sm" variant="ghost">Remove</Button>}</> : <Button disabled={saving} onClick={() => { setAddingKey(true); setManualProvider(""); setKeyInput(""); setBaseUrlInput(""); setNeedsProvider(false); setDetected(undefined); }} size="sm" variant="outline">Add key</Button>}
                  </div>}
                  {provider && editingModel === provider.id && <div className="flex w-full flex-wrap gap-2 pl-7"><select aria-label={`${provider.name} model`} className="h-9 min-w-0 flex-1 rounded-md border bg-background px-2 text-sm" onChange={(event) => setSelectedModel(event.target.value)} value={selectedModel}>{availableModels.map((model) => <option key={model} value={model}>{model}</option>)}</select><Button disabled={saving || !availableModels.includes(selectedModel)} onClick={() => void saveModel()} size="sm">Save model</Button><Button onClick={() => setEditingModel(undefined)} size="sm" variant="ghost">Cancel</Button></div>}
                </div>;
              })}
            </div>
            {saving && <p className="flex items-center gap-2 text-xs text-muted-foreground"><Spinner /> Saving…</p>}
            {addingKey ? <div className="space-y-3 rounded-lg border bg-muted/20 p-3">
              <div className="flex items-center justify-between gap-3"><div><p className="text-sm font-medium">Connect a model provider</p><p className="text-xs text-muted-foreground">Paste a key. Syda identifies and verifies it, then lists available text models.</p></div><Button onClick={() => { setAddingKey(false); setKeyInput(""); setDetected(undefined); setManualProvider(""); setNeedsProvider(false); }} size="sm" variant="ghost">Cancel</Button></div>
              <div className="flex flex-wrap gap-2"><input aria-label="API key" autoComplete="off" className="h-9 min-w-0 flex-1 rounded-md border bg-background px-3 text-sm" onChange={(event) => { setKeyInput(event.target.value); setDetected(undefined); setNeedsProvider(false); }} placeholder="Paste API key" type="password" value={keyInput} /><Button disabled={keyInput.trim().length < 8 || detecting || saving} onClick={() => void detectKey()} variant="outline">{detecting ? "Checking…" : "Detect provider"}</Button></div>
              {needsProvider && <div className="space-y-2 rounded-md border bg-background p-3"><p className="text-xs text-muted-foreground">Choose the provider for this key so Syda can verify it with the right service.</p><div className="flex flex-wrap gap-2"><select aria-label="Provider for this key" className="h-9 min-w-40 rounded-md border bg-background px-2 text-sm" onChange={(event) => setManualProvider(event.target.value)} value={manualProvider}><option value="">Choose provider</option>{settings.providers.map((provider) => <option key={provider.id} value={provider.id}>{provider.name}</option>)}</select><Button disabled={!manualProvider || (manualProvider === "openai_compatible" && !baseUrlInput.trim()) || detecting} onClick={() => void detectKey()} variant="outline">Verify key</Button></div>{manualProvider === "openai_compatible" && <input aria-label="OpenAI-compatible API base URL" className="h-9 w-full rounded-md border bg-background px-3 text-sm" onChange={(event) => setBaseUrlInput(event.target.value)} placeholder="https://your-provider.example/v1" type="url" value={baseUrlInput} />}</div>}
              {detected && <div className="space-y-3 rounded-md border border-primary bg-primary/5 p-3"><div><p className="text-sm font-medium">{detected.providerName} detected</p><p className="text-xs text-muted-foreground">Key verified · {detected.models.length} available text models</p></div><div className="flex flex-wrap gap-2"><select aria-label="Model for new key" className="h-9 min-w-0 flex-1 rounded-md border bg-background px-2 text-sm" onChange={(event) => setSelectedModel(event.target.value)} value={selectedModel}>{detected.models.map((model) => <option key={model} value={model}>{model}</option>)}</select><Button disabled={saving || !selectedModel} onClick={() => void saveKey()}>{saving ? "Saving…" : "Save key and use model"}</Button></div></div>}
              <p className="text-xs text-muted-foreground">Your API key is encrypted by the backend and never displayed again. Known key formats identify OpenAI, Anthropic, Gemini, or xAI. Other compatible services need an API base URL.</p>
            </div> : <div className="flex flex-wrap items-center justify-between gap-2"><p className="text-xs text-muted-foreground">Applies to new requests; running jobs keep their provider. Generation falls back to local synthesis if a model call fails.</p><Button onClick={() => { setAddingKey(true); setKeyInput(""); setManualProvider(""); setBaseUrlInput(""); setNeedsProvider(false); setDetected(undefined); }} size="sm" variant="outline">Add an API key</Button></div>}
          </CardContent>
        </Card>
      </>}
      {!loading && !settings && <Button variant="outline" onClick={() => window.location.reload()}>Retry</Button>}
    </div></main>
  </AppShell>;
}
