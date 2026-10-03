// Teste de seguranca da Edge Function evolution-webhook.
//
// Roda em Node (sem Deno): simula Deno.env/Deno.serve e chama o handler.
//   node supabase/functions/evolution-webhook/seguranca.test.mjs
//
// Fixa tres garantias: (1) com WORKI_WEBHOOK_SECRET definido, POST sem o
// cabecalho X-Webhook-Secret certo e recusado (401) e nada e enfileirado;
// (2) grupo e negado por padrao (lista vazia nao libera); (3) sem segredo
// configurado o comportamento antigo e mantido, e o GET avisa `segredo:false`.
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

// O index.ts e JavaScript valido; copiamos como .mjs para o Node importar.
const aqui = dirname(fileURLToPath(import.meta.url));
const dir = mkdtempSync(join(tmpdir(), 'edge-'));
const alvo = join(dir, 'fn.mjs');
writeFileSync(alvo, readFileSync(join(aqui, 'index.ts'), 'utf-8'));
let n = 0, falhas = 0;
async function carregar(env) {
  let handler;
  globalThis.Deno = { env: { get: (k) => env[k] }, serve: (h) => { handler = h; } };
  await import(pathToFileURL(alvo).href + '?v=' + (++n));
  return handler;
}
function ev(de, texto, extra = {}) {
  return [{ event: 'messages.upsert', data: { key: { remoteJid: extra.remoteJid ?? (de + '@s.whatsapp.net'), fromMe: false, id: 'X' + Math.random() }, message: { conversation: texto, from: de } } }];
}
const post = (corpo, headers = {}) => new Request('http://x/', { method: 'POST', headers: { 'content-type': 'application/json', ...headers }, body: JSON.stringify(corpo) });
const chamadas = [];
globalThis.fetch = async (url, opts) => { chamadas.push(String(url)); return new Response(JSON.stringify([{ id: 1 }]), { status: 200 }); };
const BASE = { WHATSAPP_NUMERO_AUTORIZADO: '558592494552', SUPABASE_URL: 'http://sb', SUPABASE_SERVICE_ROLE_KEY: 'k', EVOLUTION_API_URL: 'http://ev', EVOLUTION_API_KEY: 'k' };
async function caso(nome, fn) { try { await fn(); console.log('OK   ', nome); } catch (e) { falhas++; console.log('FALHA', nome, '->', e.message); } }
const enfileiradas = () => chamadas.filter((u) => u.endsWith('/rest/v1/falas')).length;

await caso('com segredo: sem cabecalho -> 401 e nada enfileirado', async () => {
  const h = await carregar({ ...BASE, WORKI_WEBHOOK_SECRET: 's3cr3t' }); chamadas.length = 0;
  const r = await h(post(ev('558592494552', 'oi')));
  assert.equal(r.status, 401); assert.equal(enfileiradas(), 0);
});
await caso('com segredo: cabecalho errado -> 401', async () => {
  const h = await carregar({ ...BASE, WORKI_WEBHOOK_SECRET: 's3cr3t' }); chamadas.length = 0;
  const r = await h(post(ev('558592494552', 'oi'), { 'x-webhook-secret': 's3cr3tX' }));
  assert.equal(r.status, 401); assert.equal(enfileiradas(), 0);
});
await caso('com segredo: cabecalho certo + numero autorizado -> enfileira', async () => {
  const h = await carregar({ ...BASE, WORKI_WEBHOOK_SECRET: 's3cr3t' }); chamadas.length = 0;
  const r = await h(post(ev('558592494552', 'oi'), { 'x-webhook-secret': 's3cr3t' }));
  assert.equal(r.status, 200); assert.equal(enfileiradas(), 1);
});
await caso('com segredo: numero NAO autorizado -> 200 mas nao enfileira', async () => {
  const h = await carregar({ ...BASE, WORKI_WEBHOOK_SECRET: 's3cr3t' }); chamadas.length = 0;
  const r = await h(post(ev('5511999999999', 'oi'), { 'x-webhook-secret': 's3cr3t' }));
  assert.equal(r.status, 200); assert.equal(enfileiradas(), 0);
});
await caso('grupo com lista VAZIA nao passa mais', async () => {
  const h = await carregar({ ...BASE, WORKI_WEBHOOK_SECRET: 's3cr3t' }); chamadas.length = 0;
  const r = await h(post(ev('558592494552', 'oi', { remoteJid: '120363001234567890@g.us' }), { 'x-webhook-secret': 's3cr3t' }));
  assert.equal(r.status, 200); assert.equal(enfileiradas(), 0);
});
await caso('grupo na lista + numero autorizado -> enfileira', async () => {
  const h = await carregar({ ...BASE, WORKI_WEBHOOK_SECRET: 's3cr3t', WHATSAPP_GRUPO_COMANDO_JIDS: '120363001234567890@g.us' }); chamadas.length = 0;
  const e = ev('558592494552', 'oi', { remoteJid: '120363001234567890@g.us' });
  const r = await h(post(e, { 'x-webhook-secret': 's3cr3t' }));
  assert.equal(r.status, 200); assert.equal(enfileiradas(), 1);
});
await caso('SEM segredo configurado: comportamento antigo (nao derruba o canal)', async () => {
  const h = await carregar({ ...BASE }); chamadas.length = 0;
  const r = await h(post(ev('558592494552', 'oi')));
  assert.equal(r.status, 200); assert.equal(enfileiradas(), 1);
});
await caso('GET health mostra segredo true/false', async () => {
  let h = await carregar({ ...BASE, WORKI_WEBHOOK_SECRET: 's3cr3t' });
  assert.equal((await (await h(new Request('http://x/', { method: 'GET' }))).json()).configurado.segredo, true);
  h = await carregar({ ...BASE });
  assert.equal((await (await h(new Request('http://x/', { method: 'GET' }))).json()).configurado.segredo, false);
});
console.log(falhas ? `\n${falhas} FALHA(S)` : '\nTODOS PASSARAM');
process.exit(falhas ? 1 : 0);
