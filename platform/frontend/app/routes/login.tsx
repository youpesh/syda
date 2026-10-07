import type { Route } from "./+types/login";
import { AuthPage } from "~/components/auth/auth-page";

export function meta({}: Route.MetaArgs) { return [{ title: "Log in — Syda" }]; }

export default function Login() { return <AuthPage mode="login" />; }
