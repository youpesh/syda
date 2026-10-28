import { redirect } from "react-router";

export function loader() {
  return redirect("/app?view=connections");
}

export default function ConnectionsRedirect() {
  return null;
}
