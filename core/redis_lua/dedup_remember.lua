-- Atomically update the dedup sliding-window state in one round-trip.
--
-- KEYS[1] = dedup key
-- ARGV[1] = original_event_id
-- ARGV[2] = now (epoch seconds, float)
-- ARGV[3] = ttl_seconds
-- ARGV[4] = reset_chain ("1" to start a fresh chain, else "0")
-- ARGV[5] = dedup_key (stored inside the payload for debugging)
-- ARGV[6] = analysis JSON string ("" if none)
--
-- Reads the current payload, bumps count and preserves first_seen_at (unless
-- reset_chain), then writes the merged payload with TTL. Doing the read +
-- modify + write inside the script makes concurrent updates for the same key
-- race-free (the previous GET-then-SETEX could lose increments under an alert
-- storm — exactly the case this dedup path exists for).

local raw = redis.call("GET", KEYS[1])
local now = tonumber(ARGV[2])
local reset_chain = ARGV[4] == "1"

local count = 1
local first_seen_at = now

if raw and not reset_chain then
    local ok, prev = pcall(cjson.decode, raw)
    if ok and type(prev) == "table" then
        if prev.count then
            count = tonumber(prev.count) + 1
        end
        if prev.first_seen_at then
            first_seen_at = tonumber(prev.first_seen_at)
        end
    end
end

local payload = {
    dedup_key = ARGV[5],
    original_event_id = tonumber(ARGV[1]),
    first_seen_at = first_seen_at,
    last_seen_at = now,
    count = count,
}

if ARGV[6] ~= "" then
    local ok, analysis = pcall(cjson.decode, ARGV[6])
    if ok then
        payload.analysis = analysis
    end
end

redis.call("SETEX", KEYS[1], tonumber(ARGV[3]), cjson.encode(payload))
return count
