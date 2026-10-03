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
  // Grupo: negado por padrao. Antes, `GRUPOS.length > 0 && ...` deixava
  // PASSAR qualquer grupo quando a lista estava vazia. Lista vazia agora
  // significa nenhum grupo permitido (AGENTS.md s.2).
  if (m.grupo && !GRUPOS.includes(m.grupo)) return 'nao_autorizado';
  return 'autorizado';
}

// Segredo do webhook (WORKI_WEBHOOK_SECRET). Sem isso, qualquer pessoa que
// conheca a URL da funcao pode forjar um POST com o numero autorizado e
// enfileirar uma fala que o relay executa no Hermes. A allowlist acima
// compara um numero que vem DENTRO do JSON, ou seja, controlado por quem
// envia: ela nao e autenticacao.
//
// Quando WORKI_WEBHOOK_SECRET esta definido, o cabecalho X-Webhook-Secret e
// obrigatorio e precisa bater. Quando nao esta definido, o comportamento
// antigo e mantido (para nao derrubar o canal de quem ainda nao configurou o
// cabecalho na Evolution) e o health check avisa `segredo: false`.
const SEGREDO = Deno.env.get('WORKI_WEBHOOK_SECRET') || '';

/** Comparacao em tempo constante, para nao vazar o segredo por tempo. */
function segredoConfere(recebido) {
  if (!SEGREDO) return true;
  const a = new TextEncoder().encode(String(recebido || ''));
  const b = new TextEncoder().encode(SEGREDO);
  let diff = a.length ^ b.length;
  const n = Math.max(a.length, b.length);
  for (let i = 0; i < n; i++) diff |= (a[i] ?? 0) ^ (b[i] ?? 0);
  return diff === 0;
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

/**
 * Enfileira a mensagem para o relay responder.
 *
 * Toda mensagem autorizada entra aqui — nao ha lista de comandos. E conversa.
 * O relay roda na maquina do Herickson, le a fila, responde e marca.
 *
 * Devolve o id da fala, ou null se falhar. Nunca lanca: uma falha aqui
 * impede a resposta, mas derrubar a request faria a Evolution repetir.
 */
async function enfileirar(numero, texto) {
  const url = Deno.env.get('SUPABASE_URL');
  const key = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY');
  if (!url || !key) {
    console.error('enfileirar: env ausente');
    return null;
  }
  try {
    const r = await fetch(`${url}/rest/v1/falas`, {
      method: 'POST',
      headers: {
        apikey: key,
        Authorization: `Bearer ${key}`,
        'Content-Type': 'application/json',
        // return=representation para pegar o id de volta. Aqui NAO vale a
        // regra do auditoria: o service_role tem SELECT, entao o 401 de RLS
        // nao acontece. Sem isso nao sabemos o id para marcar depois.
        Prefer: 'return=representation',
      },
      body: JSON.stringify({ de: numero, texto }),
    });
    if (!r.ok) {
      console.error('enfileirar falhou:', r.status, (await r.text()).slice(0, 200));
      return null;
    }
    const rows = await r.json();
    return rows?.[0]?.id ?? null;
  } catch (e) {
    console.error('enfileirar erro:', e.message);
    return null;
  }
}

Deno.serve(async (req) => {
  // CORS: a Evolution chama server-side, mas nao custa liberar.
  const cors = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
    'Access-Control-Allow-Headers': 'content-type, apikey, authorization, x-webhook-secret',
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
        // false = a funcao aceita POST de qualquer origem. Configure
        // WORKI_WEBHOOK_SECRET e o cabecalho X-Webhook-Secret na Evolution.
        segredo: Boolean(SEGREDO),
      },
    }, { headers: cors });
  }

  if (req.method !== 'POST') {
    return Response.json({ erro: 'metodo nao permitido' }, { status: 405, headers: cors });
  }

  // Autenticacao do chamador. 401 aqui e seguro: um POST sem o segredo nao e
  // a Evolution, entao nao ha entrega legitima para repetir.
  if (!segredoConfere(req.headers.get('x-webhook-secret'))) {
    console.error('webhook recusado: segredo ausente ou invalido');
    return Response.json({ ok: false, erro: 'nao autorizado' }, { status: 401, headers: cors });
  }
  if (!SEGREDO) {
    console.warn('AVISO: WORKI_WEBHOOK_SECRET nao configurado; webhook aceita qualquer origem');
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

    // ---- Conversa livre ----
    // Toda mensagem autorizada vira uma fala na fila. Nao ha lista de
    // comandos: e conversa. O relay le a fila e responde.
    //
    // 'ping' continua respondendo na hora, para provar o caminho completo
    // sem depender do relay estar no ar.
    const cmd = (m.texto || '').trim().toLowerCase();
    if (cmd === 'ping') {
      const ok = await responder(m.de, 'pong');
      if (ok) respondidos++;
      detalhes.push({ de: m.de.slice(-4), permissao });
      continue;
    }

    const fala = await enfileirar(m.de, m.texto);
    detalhes.push({ de: m.de.slice(-4), permissao, fala: fala ? 'enfileirada' : 'falhou' });
  }

  return Response.json(
    { ok: true, recebidos: eventos.length, respondidos, detalhes },
    { headers: cors },
  );
});