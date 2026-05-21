local failures = redis.call("incr", KEYS[1])
if failures == 1 then
    redis.call("expire", KEYS[1], tonumber(ARGV[1]))
end
if failures >= tonumber(ARGV[2]) then
    redis.call("set", KEYS[2], "open")
    redis.call("set", KEYS[3], ARGV[3])
    redis.call("expire", KEYS[2], tonumber(ARGV[4]))
    redis.call("expire", KEYS[3], tonumber(ARGV[4]))
    return 1
end
return 0
