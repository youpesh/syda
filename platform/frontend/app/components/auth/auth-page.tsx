import { useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import { HugeiconsIcon } from "@hugeicons/react";
import { AiMagicIcon, ArrowLeft01Icon } from "@hugeicons/core-free-icons";

import { Alert, AlertDescription, AlertTitle } from "~/components/ui/alert";
import { Button, buttonVariants } from "~/components/ui/button";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "~/components/ui/field";
import { Input } from "~/components/ui/input";
import { Spinner } from "~/components/ui/spinner";

async function responseError(response: Response, fallback: string) {
  const result = await response.json().catch(() => ({}));
  const detail = result.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((issue) => issue.msg).filter(Boolean).join(" ") || fallback;
  return fallback;
}

export function AuthPage({ mode }: { mode: "login" | "register" }) {
  const isRegister = mode === "register";
  const location = useLocation();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string>();
  const [submitting, setSubmitting] = useState(false);

  const nextPath = new URLSearchParams(location.search).get("next");
  const destination = nextPath?.startsWith("/") && !nextPath.startsWith("//") ? nextPath : "/app";

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError(undefined);
    try {
      if (isRegister) {
        const registerResponse = await fetch("/api/auth/register", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email, password, display_name: name }),
        });
        if (!registerResponse.ok) throw new Error(await responseError(registerResponse, "Could not create your account."));
      }

      const loginResponse = await fetch("/api/auth/cookie/login", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ username: email, password }),
      });
      if (!loginResponse.ok) throw new Error(await responseError(loginResponse, "Could not sign you in."));
      navigate(destination, { replace: true });
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : "Authentication failed. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return <main className="grid min-h-svh bg-background lg:grid-cols-[1fr_0.9fr]">
    <section className="relative hidden overflow-hidden bg-primary p-10 text-primary-foreground lg:flex lg:flex-col lg:justify-between xl:p-14">
      <div className="absolute -right-40 -bottom-40 size-[34rem] rounded-full border border-primary-foreground/10" />
      <div className="absolute -right-20 -bottom-20 size-[24rem] rounded-full border border-primary-foreground/10" />
      <Link className="relative z-10 flex items-center gap-2.5" to="/">
        <span className="flex size-8 items-center justify-center rounded-md bg-primary-foreground text-primary"><HugeiconsIcon icon={AiMagicIcon} strokeWidth={2} /></span>
        <span className="text-lg font-semibold tracking-tight">Syda</span>
      </Link>
      <div className="relative z-10 flex max-w-xl flex-col gap-5 pb-12">
        <p className="text-sm font-medium text-primary-foreground/70">Synthetic data, shaped around your workflow</p>
        <h1 className="text-5xl leading-[1.04] font-semibold tracking-[-0.05em] xl:text-6xl">Make every record part of the story.</h1>
        <p className="max-w-md text-base leading-7 text-primary-foreground/75">Design realistic datasets that preserve relationships, business rules, and the paths your systems take.</p>
      </div>
      <p className="relative z-10 text-xs text-primary-foreground/60">Build data you can trust to behave.</p>
    </section>

    <section className="flex min-h-svh flex-col px-5 py-6 sm:px-8 lg:px-12">
      <div className="flex items-center justify-between gap-4 lg:justify-end">
        <Link className="flex items-center gap-2 font-semibold lg:hidden" to="/"><span className="flex size-7 items-center justify-center rounded bg-primary text-primary-foreground"><HugeiconsIcon icon={AiMagicIcon} strokeWidth={2} /></span>Syda</Link>
        <Link className={buttonVariants({ variant: "ghost", size: "sm" })} to="/"><HugeiconsIcon data-icon="inline-start" icon={ArrowLeft01Icon} strokeWidth={2} /> Back to home</Link>
      </div>

      <div className="mx-auto flex w-full max-w-sm flex-1 flex-col justify-center py-12">
        <div className="mb-7 flex flex-col gap-2">
          <p className="text-xs font-medium uppercase tracking-[0.16em] text-primary">{isRegister ? "Get started" : "Welcome back"}</p>
          <h2 className="text-3xl font-semibold tracking-[-0.04em]">{isRegister ? "Create your account" : "Log in to Syda"}</h2>
          <p className="text-sm leading-6 text-muted-foreground">{isRegister ? "Set up your workspace and start shaping a scenario." : "Continue to your scenarios, runs, and data settings."}</p>
        </div>

        {error && <Alert className="mb-5" variant="destructive"><AlertTitle>Could not continue</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>}

        <form className="flex flex-col gap-5" onSubmit={submit}>
          <FieldGroup>
            {isRegister && <Field>
              <FieldLabel htmlFor="display-name">Name</FieldLabel>
              <Input autoComplete="name" id="display-name" maxLength={120} onChange={(event) => setName(event.target.value)} placeholder="Your name" required value={name} />
            </Field>}
            <Field>
              <FieldLabel htmlFor="email">Email</FieldLabel>
              <Input autoComplete="email" id="email" onChange={(event) => setEmail(event.target.value)} placeholder="you@example.com" required type="email" value={email} />
            </Field>
            <Field>
              <FieldLabel htmlFor="password">Password</FieldLabel>
              <Input autoComplete={isRegister ? "new-password" : "current-password"} id="password" minLength={10} onChange={(event) => setPassword(event.target.value)} placeholder="At least 10 characters" required type="password" value={password} />
              {isRegister && <FieldDescription>Use 10 or more characters.</FieldDescription>}
            </Field>
          </FieldGroup>
          <Button className="w-full" disabled={submitting} size="lg" type="submit">
            {submitting ? <><Spinner data-icon="inline-start" /> {isRegister ? "Creating account…" : "Logging in…"}</> : isRegister ? "Create account" : "Log in"}
          </Button>
        </form>

        <p className="mt-6 text-center text-sm text-muted-foreground">{isRegister ? "Already have an account?" : "New to Syda?"} <Link className="font-medium text-primary underline-offset-4 hover:underline" to={isRegister ? "/login" : "/register"}>{isRegister ? "Log in" : "Create an account"}</Link></p>
      </div>

      <p className="text-center text-xs text-muted-foreground">By continuing, you agree to use generated data responsibly.</p>
    </section>
  </main>;
}
