# Changelog Turnario

## 1.5 — 7 ottobre 2026
- Robustezza: un lock seriale protegge il database condiviso (il server multithread non rischia più "Recursive use of cursors" su richieste simultanee).
- POST oltre 64 KB rifiutati con 413 invece di troncare il form in silenzio.
- Correzione pagatore limitata agli eventi del gruppo aperto (un id di un altro gruppo non passa più).
- Gruppi senza nome rifiutati; PIN dell'amministratore da riga di comando con lo stesso controllo (4 cifre).
- Pagina membri: mostra davvero i messaggi di conferma/errore ("aggiunto", "PIN: 4 cifre"...).
- Date nulle non provocano più errori 500.
- Refuso: "lo storico resta in archivio".
## 1.4 — 7 ottobre 2026
- Revisione di sicurezza: il limitatore di tentativi di login usa l'IP reale del client (`X-Forwarded-For`), non più `127.0.0.1` dietro Caddy: un utente bloccato non blocca tutti.
- Dimensione massima dei POST (64 KB).
- Header di sicurezza su ogni risposta: `X-Content-Type-Options: nosniff`, `Cache-Control: no-store` (Caddy aggiunge HSTS e `X-Frame-Options`).
- Cookie di sessione con flag `Secure` quando la connessione è HTTPS.
- Fingerprint del server nascosto: `Server: Turnario/1.4` invece di `Python/3.x http.server`.
- Il PIN non è più ripetuto nelle pagine di amministrazione dopo la creazione o il cambio.
- Nomi dei presenti nel registro escapati HTML; date e id malformati non provocano più errori 500.

## 1.3 — 7 ottobre 2026
- Pagine di errore ("Solo l'admin", "Gruppo non trovato", 404) con la stessa grafica delle altre pagine e un pulsante per tornare alla home.

## 1.2 — 7 ottobre 2026
- Pagina changelog: barra di navigazione in alto (logo per tornare alla home, logout).

## 1.1 — 7 ottobre 2026
- Amministrazione: rinominare un gruppo.
- Amministrazione: eliminare un gruppo (vai in archivio: il storico resta, il gruppo non è più raggiungibile) e riattivarlo.
- Amministrazione: eliminare un utente (con gli eventi in cui ha partecipato).
- Amministrazione: cambiare il PIN di un utente.
- Numero di versione visibile in fondo a ogni pagina; questa pagina changelog su `/changelog`.
- Registro eventi della produzione svuotato: tutti i debiti sono ripartiti da zero.

## 1.0 — 7 ottobre 2026
- Sorteggio deterministico: paga chi ha il debito più alto, in caso di parità sorteggio casuale.
- Debito = presenze − pagamenti, cumulativo, senza reset.
- Due registri per gruppo: caffè e macchina.
- Login con PIN (4 cifre), cookie di sessione, limitazione tentativi.
- Correzione del pagatore "in realtà ha pagato Y" dopo il sorteggio.
- Notifica Telegram quando un turno viene registrato.
