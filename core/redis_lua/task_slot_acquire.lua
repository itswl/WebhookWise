redis.call("zremrangebyscore", KEYS[1], "-inf", ARGV[1])
if redis.call("zcard", KEYS[1]) >= tonumber(ARGV[2]) then
    return 0
end
redis.call("zadd", KEYS[1], ARGV[3], ARGV[4])
redis.call("pexpire", KEYS[1], ARGV[5])
return 1
