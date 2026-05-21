local current = tonumber(redis.call("get", KEYS[1]) or "0")
local threshold = tonumber(ARGV[2])
if threshold > 0 and current >= threshold then
    return -current
end
local c = redis.call("incr", KEYS[1])
redis.call("expire", KEYS[1], tonumber(ARGV[1]))
return c
