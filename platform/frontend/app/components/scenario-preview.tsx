import { Input } from "~/components/ui/input";
import { Textarea } from "~/components/ui/textarea";

export type ScenarioDraft = { dataset: "commerce" | "healthcare"; cancerTypes: string; demographics: string; workflow: string; rules: string };
export const defaultDraft: ScenarioDraft = { dataset: "commerce", cancerTypes: "Lung, colorectal", demographics: "Adults aged 40–80; specify the desired demographic distribution", workflow: "Patient → Diagnosis → Claim → Payment", rules: "Diagnosis precedes claim; denied claims have no payment." };
export const sampleFiles: Record<ScenarioDraft["dataset"], Record<string, string>> = {
  commerce: {
    customers: "customer_id,name\n1,Sample Customer A\n2,Sample Customer B\n",
    orders: "order_id,customer_id,amount\n101,1,24.50\n102,2,18.00\n",
  },
  healthcare: {
    patients: "patient_id,age,gender\n1,58,female\n2,64,male\n",
    diagnoses: "diagnosis_id,patient_id,cancer_type,diagnosis_date\n11,1,lung,2026-01-02\n12,2,colorectal,2026-01-03\n",
    claims: "claim_id,patient_id,diagnosis_id,claim_date,status\n21,1,11,2026-01-05,approved\n22,2,12,2026-01-06,denied\n",
    payments: "payment_id,claim_id,amount,payment_date\n31,21,120.00,2026-01-10\n",
  },
};
export function ScenarioFields({value,onChange}:{value:ScenarioDraft;onChange:(next:ScenarioDraft)=>void}) {
  return <section className="flex flex-col gap-4 border-y py-4" aria-label="Scenario details">
    <label className="flex flex-col gap-2 text-xs">Scenario dataset<select className="h-8 border bg-background px-2" value={value.dataset} onChange={e=>onChange({...value,dataset:e.target.value as ScenarioDraft["dataset"]})}><option value="commerce">Commerce</option><option value="healthcare">Healthcare</option></select></label>
    {value.dataset === "healthcare" && <><label className="flex flex-col gap-2 text-xs">Cancer types<Input value={value.cancerTypes} onChange={e=>onChange({...value,cancerTypes:e.target.value})}/></label><label className="flex flex-col gap-2 text-xs">Demographic requirements<Textarea value={value.demographics} onChange={e=>onChange({...value,demographics:e.target.value})}/></label><label className="flex flex-col gap-2 text-xs">Workflow steps<Input value={value.workflow} onChange={e=>onChange({...value,workflow:e.target.value})}/></label><label className="flex flex-col gap-2 text-xs">Custom rules<Textarea value={value.rules} onChange={e=>onChange({...value,rules:e.target.value})}/></label><p className="text-xs text-muted-foreground">These requirements are saved with the run. Preview files are fixed illustrative samples; they do not adapt to these fields or validate clinical rules.</p></>}
  </section>;
}
