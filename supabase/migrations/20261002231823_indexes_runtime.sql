create index if not exists idx_acoes_tarefa on public.acoes_pendentes(tarefa_id);
create index if not exists idx_memorias_substitui on public.memorias(substitui_id);
create index if not exists idx_conteudos_concorrente on public.conteudos_concorrentes(concorrente_id);
