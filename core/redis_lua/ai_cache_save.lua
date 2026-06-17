-- Atomically write both AI-cache keys in one round-trip.
--
-- KEYS[1] = cache key (analysis blob)
-- KEYS[2] = hit-counter key
-- ARGV[1] = ttl_seconds
-- ARGV[2] = cached analysis bytes/JSON
--
-- Replaces two serial SETEX calls (blob + "0" counter) with a single script so
-- the write costs one round-trip instead of two. The two keys share the TTL.

local ttl = tonumber(ARGV[1])
redis.call("SETEX", KEYS[1], ttl, ARGV[2])
redis.call("SETEX", KEYS[2], ttl, "0")
return 1
