if redis.call("zscore", KEYS[1], ARGV[1]) then
    redis.call("zadd", KEYS[1], ARGV[2], ARGV[1])
    redis.call("pexpire", KEYS[1], ARGV[3])
    return 1
end
return 0
