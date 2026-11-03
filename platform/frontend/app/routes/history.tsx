import { redirect } from "react-router";

export function loader() {
  return redirect("/app?view=runs");
}

export default function HistoryRedirect() {
  return null;
}
