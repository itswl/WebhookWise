-- Multi-tier sliding-window rate limit in a single round-trip.
--
-- KEYS: one prefix per tier (e.g. burst / sustained / global).
-- ARGV: now, then for each tier a (window, limit) pair in KEYS order:
--   ARGV[1] = now
--   ARGV[2*i]   = window_i
--   ARGV[2*i+1] = limit_i
--
-- Returns a flat array: { failed_index, r1, r2, ... }
--   failed_index = 0 if all tiers allow, else the 1-based index of the first
--                  tier over its limit (no counters are incremented in that case).
--   r_i          = remaining budget for tier i (>=0). Only meaningful when
--                  failed_index == 0; for a rejection the caller just needs the
--                  failed tier.
--
-- Checking every tier before incrementing any avoids the partial-increment bug
-- of incrementing earlier tiers when a later tier rejects.

local now = tonumber(ARGV[1])
local n = #KEYS

local remaining = {}
local current_keys = {}

for i = 1, n do
    local prefix = KEYS[i]
    local window = tonumber(ARGV[2 * i])
    local limit = tonumber(ARGV[2 * i + 1])

    local current_window = math.floor(now / window)
    local previous_window = current_window - 1
    local current_key = prefix .. ":" .. current_window
    local previous_key = prefix .. ":" .. previous_window

    local current_count = tonumber(redis.call("GET", current_key) or "0")
    local previous_count = tonumber(redis.call("GET", previous_key) or "0")

    local elapsed = now - current_window * window
    local weight = (window - elapsed) / window
    local estimated = math.floor(previous_count * weight + current_count)

    if estimated >= limit then
        return { i }
    end

    current_keys[i] = current_key
    remaining[i] = limit - estimated - 1
end

-- All tiers allow: now commit the increments.
for i = 1, n do
    local window = tonumber(ARGV[2 * i])
    redis.call("INCR", current_keys[i])
    redis.call("EXPIRE", current_keys[i], window * 2)
end

local result = { 0 }
for i = 1, n do
    result[i + 1] = remaining[i]
end
return result
