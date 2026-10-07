import type { Route } from "./+types/register";
import { AuthPage } from "~/components/auth/auth-page";

export function meta({}: Route.MetaArgs) { return [{ title: "Create account — Syda" }]; }

export default function Register() { return <AuthPage mode="register" />; }
