-- Like the other functions: only the service role (Edge Function, scripts) calls them.
revoke execute on function public.project_budget(text) from public, anon, authenticated;
revoke execute on function public.later_stage_budget(text, text) from public, anon, authenticated;
grant execute on function public.project_budget(text) to service_role;
grant execute on function public.later_stage_budget(text, text) to service_role;
