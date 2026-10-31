-- Reserve worst-case cost against the team AND user monthly budgets, atomically, and open a
-- ledger entry for the request. Single-node (cluster mode disabled) Valkey.
-- KEYS[1] team spent   KEYS[2] user spent   KEYS[3] user rpm window
-- KEYS[4] hold record (hash)                KEYS[5] deadline index (zset)
-- ARGV[1] amount  ARGV[2] team limit  ARGV[3] user limit  ARGV[4] rpm limit  ARGV[5] budget ttl
-- ARGV[6] now     ARGV[7] deadline    ARGV[8] request id  ARGV[9] team       ARGV[10] user
-- Returns {ok, reason, team_spent, user_spent}
local team = tonumber(redis.call('GET', KEYS[1]) or '0')
local user = tonumber(redis.call('GET', KEYS[2]) or '0')

-- Idempotency: the same request id never holds budget twice.
local state = redis.call('HGET', KEYS[4], 'state')
if state then
  if state == 'reserved' or state == 'invoking' then return {0, 'duplicate_in_flight', team, user} end
  return {0, 'duplicate_completed', team, user}
end

local n = redis.call('INCR', KEYS[3])
if n == 1 then redis.call('EXPIRE', KEYS[3], 60) end
if n > tonumber(ARGV[4]) then return {0, 'rate_limited', team, user} end

local amount = tonumber(ARGV[1])
if team + amount > tonumber(ARGV[2]) then return {0, 'team_budget_exceeded', team, user} end
if user + amount > tonumber(ARGV[3]) then return {0, 'user_budget_exceeded', team, user} end

team = redis.call('INCRBY', KEYS[1], amount)
redis.call('EXPIRE', KEYS[1], ARGV[5])
user = redis.call('INCRBY', KEYS[2], amount)
redis.call('EXPIRE', KEYS[2], ARGV[5])
redis.call('HSET', KEYS[4], 'state', 'reserved', 'amount', amount, 'team_key', KEYS[1], 'user_key', KEYS[2],
           'created', ARGV[6], 'deadline', ARGV[7], 'team', ARGV[9], 'user', ARGV[10])
redis.call('EXPIRE', KEYS[4], ARGV[5])
redis.call('ZADD', KEYS[5], ARGV[7], ARGV[8])
return {1, 'ok', team, user}
