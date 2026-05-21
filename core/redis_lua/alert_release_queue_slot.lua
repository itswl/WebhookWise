local current = tonumber(redis.call("get", KEYS[1]) or "0")
if current <= 1 then
    redis.call("del", KEYS[1])
    return 0
end
local c = redis.call("decr", KEYS[1])
redis.call("expire", KEYS[1], tonumber(ARGV[1]))
return c
