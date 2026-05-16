# Polymarket Insider Finder

Este script busca señales compatibles con flujo informado en Polymarket usando una heurística simple:

- sube con fuerza el `Open Interest` del evento
- al mismo tiempo el precio del `YES` o del `NO` se mueve de forma agresiva

No prueba insider trading. Solo marca anomalías que merecen revisión manual.

## Cómo funciona

1. Lee los mercados activos desde la API pública de Gamma.
2. Se queda solo con mercados binarios `Yes/No`.
3. Guarda snapshots en SQLite para comparar cada iteración con la anterior.
4. Aplica perfiles distintos por `feeType` y por banda de liquidez para ajustar sensibilidad.
5. Agrupa por evento porque el `openInterest` público de Gamma viene a nivel de evento, no de pregunta individual.
6. Dentro de cada evento, selecciona la pregunta binaria con el mayor movimiento de precio para representar la señal.

## Recomendación de frecuencia

- `60` segundos es el mejor punto de partida.
- `30` segundos tiene sentido solo en ventanas de noticias o mercados muy calientes.
- `120` segundos reduce ruido si quieres un monitor de fondo.

La recomendación práctica es empezar en `60` segundos, porque por debajo de eso aumenta bastante el ruido y repites muchos estados de mercado sin ganar demasiada información marginal.

## Uso

Primera pasada para crear baseline:

```bash
python3 polymarket_insider_finder.py
```

Monitor continuo cada 60 segundos:

```bash
python3 polymarket_insider_finder.py --watch --interval 60
```

Ajustar sensibilidad:

```bash
python3 polymarket_insider_finder.py \
  --watch \
  --interval 60 \
  --min-oi-abs 8000 \
  --min-oi-pct 0.05 \
  --min-price-move 0.08
```

Monitor como servicio con logs rotativos:

```bash
python3 polymarket_insider_finder.py --service --telegram --interval 60
```

Generar plist de `launchd` para macOS:

```bash
python3 polymarket_insider_finder.py --write-launchd-plist --telegram --interval 60
```

Mensaje de prueba a Telegram:

```bash
python3 polymarket_insider_finder.py --telegram --telegram-test-message "Prueba Insider Finder"
```

Si tienes las credenciales guardadas en `config/telegram.env`, el programa las carga automáticamente. No hace falta exportarlas a mano para uso normal.

## Umbrales por defecto

- `--min-oi-abs 5000`
- `--min-oi-pct 0.04`
- `--min-price-move 0.06`
- `--min-liquidity 2000`
- `--min-volume-24h 250`

Esos valores son la base global. Encima de eso el script puede endurecer o relajar umbrales según:

- `feeType`, como `general_fees`, `culture_fees` o `sports_fees_v2`
- bandas de liquidez, para no tratar igual un mercado de `$4K` y uno de `$400K`

La configuración vive en `config/signal_rules.json`.

## Telegram

Para activar alertas necesitas:

- `POLYMARKET_TELEGRAM_BOT_TOKEN`
- `POLYMARKET_TELEGRAM_CHAT_ID`

Puedes guardarlos en `config/telegram.env` así:

```text
POLYMARKET_TELEGRAM_BOT_TOKEN=tu_token
POLYMARKET_TELEGRAM_CHAT_ID=tu_chat_id
```

El sistema deduplica alertas por mercado y dirección (`YES` o `NO`) usando un cooldown configurable con `--notification-cooldown`.

## Servicio y logs

- El modo `--service` activa `watch` y escribe logs con rotación en `logs/polymarket_insider_finder.log`.
- El `plist` generado queda por defecto en `launchd/com.fernandozamora.polymarket-insider-finder.plist`.
- El servicio se mantiene vivo y el propio proceso hace un sondeo cada `--interval` segundos. Con la configuración actual, la frecuencia es de `60` segundos.
- Si usas `config/telegram.env`, `launchd` no necesita `launchctl setenv`; el script lo lee directamente al arrancar.
- Para pararlo de verdad no basta con matar el proceso, porque `KeepAlive` lo levantaría otra vez. Hay que descargar el agente con `launchctl bootout`.

## Persistencia local

Los snapshots se guardan en:

```text
data/polymarket_insider.sqlite3
```

El mismo SQLite también guarda el historial de alertas enviadas para no repetir notificaciones idénticas en Telegram.

## Limitación importante

La API pública de Gamma expone `openInterest` en el objeto `event`. Eso significa que, en eventos con varias preguntas, la señal de capital fresco se detecta a nivel del evento y luego se asocia a la pregunta binaria con mayor desplazamiento de precio. Es una aproximación útil, pero no una prueba forense por mercado.

Además, Gamma no expone una categoría de mercado limpia y consistente en estos endpoints públicos, así que el ajuste por "categoría" se hace usando `feeType` como proxy operativo.
