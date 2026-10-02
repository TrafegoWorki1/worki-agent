// Webhook receptor da Evolution API.
//
// Evolution envia MESSAGES_UPSERT -> esta funcao valida a allowlist, grava
// auditoria e responde. Tudo em uma request: sem Database Webhook, sem Vercel,
// sem polling.
//
// Regra critica: devolve 200 SEMPRE. Um 4xx faz a Evolution repetir a entrega e
// transformar um numero nao autorizado em fila infinita.
//
// Variaveis (supabase secrets set):
//   EVOLUTION_API_URL, EVOLUTION_API_KEY, EVOLUTION_INSTANCE,
//   WHATSAPP_NUMERO_AUTORIZADO, WHATSAPP_GRUPO_COMANDO_JIDS,
//   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY  (esta ultima o Supabase injeta)

const ALLOW = (Deno.env.get('WHATSAPP_NUMERO_AUTORIZADO') || '').replace(/\D/g, '');
const GRUPOS = (Deno.env.get('WHATSAPP_GRUPO_COMANDO_JIDS') || '')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);

const INSTANCE = Deno.env.get('EVOLUTION_INSTANCE') || 'agent-domintante';

function digitos(jid) {
  return String(jid || '').replace(/\D/g, '');
}

/** Normaliza o payload da Evolution. Numeros vem em formatos diferentes. */
function extrair(evento) {
  const dados = evento?.data ?? {};
  const key = dados.key ?? {};
  const msg = dados.message ?? {};

  const remoteJid = dados.remoteJid ?? key.remoteJid ?? '';
  const de = digitos(msg.from ?? remoteJid);
  const grupo = String(remoteJid).endsWith('@g.us') ? remoteJid : null;

  let texto = '';
  if (typeof msg.conversation === 'string') texto = msg.conversation;
  else if (typeof msg.extendedTextMessage?.text === 'string') texto = msg.extendedTextMessage.text;
  else if (typeof msg.imageMessage?.caption === 'string') texto = msg.imageMessage.caption;
  else if (msg.buttonResponseMessage) texto = '[botao]';
  else if (msg.listResponseMessage) texto = '[lista]';

  return {
    de,
    grupo,
    texto,
    deMim: Boolean(key.fromMe),
    tipoMensagem: Object.keys(msg)[0] ?? null,
    id: key.id ?? null,
    pushName: dados.pushName ?? null,
  };
}

/**
 * Allowlist. Retorna 'autorizado' | 'nao_autorizado'.
 * O chip do agente NAO esta na allowlist: ele recebe e responde, nao comanda.
 */
function checarAllowlist(m) {
  if (m.deMim) return 'nao_autorizado';          // mensagem que eu enviei
  if (!ALLOW) return 'erro_config';                // allowlist nao configurada
  if (m.de !== ALLOW) return 'nao_autorizado';     // so seu numero comanda
  if (m.grupo && GRUPOS.length > 0 && !GRUPOS.includes(m.grupo)) return 'nao_autorizado';
  return 'autorizado';
}

/** Grava auditoria. Nunca lanca: falha aqui nao pode derrubar o canal. */
async function auditar(registro) {
  const url = Deno.env.get('SUPABASE_URL');
  const key = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY');
  if (!url || !key) {
    console.error('auditoria: env ausente');
    return;
  }
  try {
    const r = await fetch(`${url}/rest/v1/auditoria`, {
      method: 'POST',
      headers: {
        apikey: key,
        Authorization: `Bearer ${key}`,
        'Content-Type': 'application/json',
        // return=minimal, nunca representation: representation exige SELECT
        // junto e devolve 401 "violates row-level security policy" mesmo
        // com a policy correta. Ver docs/rls-armadilhas.md
        Prefer: 'return=minimal',
      },
      body: JSON.stringify(registro),
    });
    if (!r.ok) console.error('auditoria falhou:', r.status, (await r.text()).slice(0, 200));
  } catch (e) {
    console.error('auditoria erro:', e.message);
  }
}

/** Responde no WhatsApp via Evolution. */
async function responder(numero, texto) {
  const base = Deno.env.get('EVOLUTION_API_URL');
  const key = Deno.env.get('EVOLUTION_API_KEY');
  if (!base || !key) {
    console.error('responder: EVOLUTION_API_URL/KEY ausentes');
    return false;
  }
  try {
    const r = await fetch(`${base.replace(/\/$/, '')}/message/sendText/${INSTANCE}`, {
      method: 'POST',
      headers: { apikey: key, 'Content-Type': 'application/json' },
      body: JSON.stringify({ number: numero, text: texto }),
    });
    if (!r.ok) {
      console.error('sendText falhou:', r.status, (await r.text()).slice(0, 200));
      return false;
    }
    return true;
  } catch (e) {
    console.error('sendText erro:', e.message);
    return false;
  }
}

Deno.serve(async (req) => {
  // CORS: a Evolution chama server-side, mas nao custa liberar.
  const cors = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
    'Access-Control-Allow-Headers': 'content-type, apikey, authorization',
  };
  if (req.method === 'OPTIONS') return new Response(null, { status: 204, headers: cors });

  // GET: health check.
  if (req.method === 'GET') {
    return Response.json({
      ok: true,
      configurado: {
        allowlist: Boolean(ALLOW),
        grupos: GRUPOS.length,
        evolution: Boolean(Deno.env.get('EVOLUTION_API_URL')),
        instance: INSTANCE,
      },
    }, { headers: cors });
  }

  if (req.method !== 'POST') {
    return Response.json({ erro: 'metodo nao permitido' }, { status: 405, headers: cors });
  }

  const t0 = Date.now();
  let eventos;
  try {
    const body = await req.json();
    eventos = Array.isArray(body) ? body : [body];
  } catch {
    // corpo invalido: 200 para a Evolution nao repetir
    return Response.json({ ok: false, erro: 'json invalido' }, { headers: cors });
  }

  let respondidos = 0;
  const detalhes = [];

  for (const evento of eventos) {
    const m = extrair(evento);
    const permissao = checarAllowlist(m);

    await auditar({
      canal: 'whatsapp',
      remetente: m.de ? `${m.de.slice(0, 2)}***${m.de.slice(-4)}` : null,
      comando: m.texto ? m.texto.slice(0, 200) : null,
      skill: 'webhook-evolution',
      ferramenta: 'supabase:edge-function',
      resultado: permissao,
      aguardou_aprovacao: false,
      duracao_ms: Date.now() - t0,
    });

    if (permissao !== 'autorizado') {
      detalhes.push({ de: m.de ? `${m.de.slice(-4)}` : null, permissao });
      continue;
    }

    // ---- Fase 1: criterio de pronto ----
    const cmd = (m.texto || '').trim().toLowerCase();
    if (cmd === 'ping') {
      const ok = await responder(m.de, 'pong');
      if (ok) respondidos++;
    }
    // Futuras skills entram aqui.

    detalhes.push({ de: m.de.slice(-4), permissao });
  }

  return Response.json(
    { ok: true, recebidos: eventos.length, respondidos, detalhes },
    { headers: cors },
  );
});