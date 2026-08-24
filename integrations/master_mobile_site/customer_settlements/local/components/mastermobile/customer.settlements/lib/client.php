<?php

declare(strict_types=1);

namespace MasterMobile\CustomerSettlements;

final class Client
{
    private const DEFAULT_CONFIG = '/etc/master-mobile/customer-settlements.json';
    private const SUMMARY_PATH = '/api/customer/settlements/summary';
    private const ELIGIBILITY_PATH = '/api/customer/settlements/eligibility';
    private const USER_STATUSES = array(
        'available',
        'stale',
        'temporarily_unavailable',
        'not_linked',
        'ambiguous_link',
        'pilot_disabled',
    );
    private const BALANCE_STATES = array('debt', 'advance', 'zero');

    /** @var array<string,array<string,mixed>> */
    private static array $requestCache = array();

    private function __construct()
    {
    }

    /** @return array<string,mixed> */
    public static function summaryForUser(
        string $siteUserId,
        bool $probe = false,
        ?string $mockVariant = null
    ): array {
        if (!preg_match('/^[1-9][0-9]{0,18}$/', $siteUserId)) {
            return self::unavailable('invalid_site_user');
        }

        try {
            $config = self::loadConfig();
            $mode = (string)($config['mode'] ?? 'off');
            $cacheKey = $siteUserId . '|' . ($probe ? 'probe' : 'full') . '|' . (string)$mockVariant;
            if (isset(self::$requestCache[$cacheKey])) {
                return self::$requestCache[$cacheKey];
            }

            if ($mode === 'mock') {
                if (!self::mockUserAllowed($config, $siteUserId)) {
                    $result = array('status' => 'pilot_disabled', 'is_stale' => false);
                } else {
                    $variant = $mockVariant ?: (string)($config['mock_variant'] ?? 'debt');
                    $result = self::mockSummary($variant);
                }
            } elseif ($mode === 'real') {
                $result = self::fetchRealSummary($config, $siteUserId, $probe);
            } else {
                $result = self::unavailable('feature_off', true);
            }
            self::$requestCache[$cacheKey] = $result;
            return $result;
        } catch (\Throwable $error) {
            self::safeLog('client_failure');
            return self::unavailable('client_failure');
        }
    }

    /** @return array{status:string} */
    public static function eligibilityForUser(string $siteUserId): array
    {
        if (!preg_match('/^[1-9][0-9]{0,18}$/', $siteUserId)) {
            return array('status' => 'not_eligible');
        }
        try {
            $config = self::loadConfig();
            $mode = (string)($config['mode'] ?? 'off');
            $cacheKey = $siteUserId . '|eligibility';
            if (isset(self::$requestCache[$cacheKey])) {
                return self::$requestCache[$cacheKey];
            }
            if ($mode === 'mock') {
                $result = array(
                    'status' => self::mockUserAllowed($config, $siteUserId)
                        ? 'eligible'
                        : 'not_eligible',
                );
            } elseif ($mode === 'real') {
                $payload = self::fetchBackendJson(
                    $config,
                    $siteUserId,
                    self::ELIGIBILITY_PATH,
                    true
                );
                $status = (string)($payload['status'] ?? '');
                if (!in_array($status, array(
                    'eligible', 'not_eligible', 'temporarily_unavailable',
                ), true)) {
                    throw new \RuntimeException('eligibility_status_invalid');
                }
                $result = array('status' => $status);
            } else {
                $result = array('status' => 'not_eligible');
            }
            self::$requestCache[$cacheKey] = $result;
            return $result;
        } catch (\Throwable $error) {
            self::safeLog('eligibility_failure');
            return array('status' => 'temporarily_unavailable');
        }
    }

    /** @return array<string,mixed> */
    public static function loadConfig(?string $path = null): array
    {
        $configPath = $path;
        if ($configPath === null || $configPath === '') {
            $configured = getenv('MM_CUSTOMER_SETTLEMENTS_CONFIG');
            $configPath = is_string($configured) && $configured !== ''
                ? $configured
                : self::DEFAULT_CONFIG;
        }
        if (!is_file($configPath) || !is_readable($configPath)) {
            return array('mode' => 'off');
        }
        $raw = file_get_contents($configPath);
        $decoded = is_string($raw) ? json_decode($raw, true) : null;
        if (!is_array($decoded)) {
            throw new \RuntimeException('invalid_config');
        }
        return $decoded;
    }

    public static function buildAssertion(
        array $config,
        string $siteUserId,
        int $now,
        string $jti
    ): string {
        $kid = (string)($config['active_kid'] ?? '');
        $secret = (string)($config['active_secret'] ?? '');
        if ($kid === '' || strlen($secret) < 16 || $jti === '') {
            throw new \RuntimeException('assertion_config_invalid');
        }
        $header = array(
            'alg' => 'HS256',
            'typ' => 'MM-CUSTOMER-SETTLEMENTS',
            'kid' => $kid,
        );
        $payload = array(
            'iss' => (string)($config['issuer'] ?? 'master-mobile.ru'),
            'aud' => (string)($config['audience'] ?? 'pricing-service:customer-settlements'),
            'sub' => $siteUserId,
            'site_user_id' => $siteUserId,
            'scope' => (string)($config['scope'] ?? 'customer:settlements:read'),
            'iat' => $now,
            'nbf' => $now,
            'exp' => $now + 60,
            'jti' => $jti,
        );
        $encodedHeader = self::base64Url(self::encodeJson($header));
        $encodedPayload = self::base64Url(self::encodeJson($payload));
        $signingInput = $encodedHeader . '.' . $encodedPayload;
        $signature = self::base64Url(hash_hmac('sha256', $signingInput, $secret, true));
        return $signingInput . '.' . $signature;
    }

    /** @return array<string,mixed> */
    public static function normalizeSummary(array $payload): array
    {
        $status = (string)($payload['status'] ?? '');
        if (!in_array($status, self::USER_STATUSES, true)) {
            throw new \RuntimeException('response_status_invalid');
        }
        $isStale = $payload['is_stale'] ?? null;
        if (!is_bool($isStale)) {
            throw new \RuntimeException('response_stale_invalid');
        }
        if (($status === 'stale' && !$isStale) || ($status === 'available' && $isStale)) {
            throw new \RuntimeException('response_stale_status_mismatch');
        }
        $result = array('status' => $status, 'is_stale' => $isStale);
        if ($status === 'available' || $status === 'stale') {
            $state = (string)($payload['state'] ?? '');
            $amount = (string)($payload['amount'] ?? '');
            if (!in_array($state, self::BALANCE_STATES, true)) {
                throw new \RuntimeException('response_state_invalid');
            }
            if (!preg_match('/^(0|[1-9][0-9]*)\.[0-9]{2}$/', $amount)) {
                throw new \RuntimeException('response_amount_invalid');
            }
            if (($payload['currency'] ?? null) !== 'RUB') {
                throw new \RuntimeException('response_currency_invalid');
            }
            self::requireIsoDate((string)($payload['as_of'] ?? ''));
            self::requireIsoDate((string)($payload['synced_at'] ?? ''));
            $result += array(
                'state' => $state,
                'amount' => $amount,
                'currency' => 'RUB',
                'as_of' => (string)$payload['as_of'],
                'synced_at' => (string)$payload['synced_at'],
            );
        }
        return $result;
    }

    /** @return array<string,mixed> */
    public static function mockSummary(string $variant): array
    {
        $base = array(
            'amount' => '14800.00',
            'currency' => 'RUB',
            'as_of' => '2026-08-22T16:30:00Z',
            'synced_at' => '2026-08-22T16:34:12Z',
            'is_stale' => false,
        );
        $variants = array(
            'debt' => array('status' => 'available', 'state' => 'debt') + $base,
            'advance' => array('status' => 'available', 'state' => 'advance') + $base,
            'zero' => array('status' => 'available', 'state' => 'zero', 'amount' => '0.00') + $base,
            'stale' => array('status' => 'stale', 'state' => 'debt', 'is_stale' => true) + $base,
            'unavailable' => array('status' => 'temporarily_unavailable', 'is_stale' => false),
            'not_linked' => array('status' => 'not_linked', 'is_stale' => false),
            'ambiguous' => array('status' => 'ambiguous_link', 'is_stale' => false),
            'disabled' => array('status' => 'pilot_disabled', 'is_stale' => false),
        );
        return self::normalizeSummary($variants[$variant] ?? $variants['debt']);
    }

    public static function isMockModeForHost(string $host): bool
    {
        try {
            $config = self::loadConfig();
            return ($config['mode'] ?? null) === 'mock'
                && strtolower(preg_replace('/:\d+$/', '', $host)) === 'dev.master-mobile.ru';
        } catch (\Throwable $error) {
            return false;
        }
    }

    public static function mockQueryEnabledForHost(string $host): bool
    {
        try {
            $config = self::loadConfig();
            return self::isMockModeForHost($host)
                && ($config['mock_query_enabled'] ?? false) === true;
        } catch (\Throwable $error) {
            return false;
        }
    }

    /** @param array<string,mixed> $config */
    private static function mockUserAllowed(array $config, string $siteUserId): bool
    {
        $salt = (string)($config['mock_user_hash_salt'] ?? '');
        $allowed = $config['mock_allowed_user_hashes'] ?? null;
        if ($salt === '' || !is_array($allowed)) {
            return false;
        }
        $candidate = hash_hmac('sha256', $siteUserId, $salt);
        foreach ($allowed as $value) {
            if (is_string($value) && hash_equals(strtolower($value), $candidate)) {
                return true;
            }
        }
        return false;
    }

    /** @return array<string,mixed> */
    private static function fetchRealSummary(array $config, string $siteUserId, bool $probe): array
    {
        $decoded = self::fetchBackendJson(
            $config,
            $siteUserId,
            self::SUMMARY_PATH,
            $probe
        );
        try {
            return self::normalizeSummary($decoded);
        } catch (\Throwable $error) {
            self::safeLog('backend_contract_failure');
            return self::unavailable('backend_contract_failure');
        }
    }

    /** @return array<string,mixed> */
    private static function fetchBackendJson(
        array $config,
        string $siteUserId,
        string $path,
        bool $probe
    ): array {
        $baseUrl = rtrim((string)($config['base_url'] ?? ''), '/');
        $parts = parse_url($baseUrl);
        $allowedHosts = $config['allowed_hosts'] ?? array();
        if (
            !is_array($parts)
            || ($parts['scheme'] ?? null) !== 'https'
            || empty($parts['host'])
            || isset($parts['user'])
            || isset($parts['pass'])
            || !is_array($allowedHosts)
            || !in_array(strtolower((string)$parts['host']), array_map('strtolower', $allowedHosts), true)
        ) {
            throw new \RuntimeException('base_url_invalid');
        }
        if (!function_exists('curl_init')) {
            throw new \RuntimeException('curl_unavailable');
        }
        $jti = self::base64Url(random_bytes(24));
        $assertion = self::buildAssertion($config, $siteUserId, time(), $jti);
        $curl = curl_init($baseUrl . $path);
        if ($curl === false) {
            throw new \RuntimeException('curl_init_failed');
        }
        curl_setopt_array($curl, array(
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_FOLLOWLOCATION => false,
            CURLOPT_CONNECTTIMEOUT_MS => $probe ? 500 : 2000,
            CURLOPT_TIMEOUT_MS => $probe ? 1000 : 3000,
            CURLOPT_SSL_VERIFYPEER => true,
            CURLOPT_SSL_VERIFYHOST => 2,
            CURLOPT_HTTPHEADER => array(
                'Authorization: Bearer ' . $assertion,
                'Accept: application/json',
            ),
        ));
        $body = curl_exec($curl);
        $status = (int)curl_getinfo($curl, CURLINFO_RESPONSE_CODE);
        curl_close($curl);
        if ($status !== 200 || !is_string($body)) {
            self::safeLog('backend_http_failure');
            throw new \RuntimeException('backend_http_failure');
        }
        $decoded = json_decode($body, true);
        if (!is_array($decoded)) {
            self::safeLog('backend_json_failure');
            throw new \RuntimeException('backend_json_failure');
        }
        return $decoded;
    }

    /** @return array<string,mixed> */
    private static function unavailable(string $code, bool $disabled = false): array
    {
        return array(
            'status' => $disabled ? 'pilot_disabled' : 'temporarily_unavailable',
            'is_stale' => false,
            '_transport_error' => !$disabled,
            '_error_code' => $code,
        );
    }

    private static function requireIsoDate(string $value): void
    {
        try {
            new \DateTimeImmutable($value);
        } catch (\Throwable $error) {
            throw new \RuntimeException('response_date_invalid');
        }
    }

    private static function encodeJson(array $value): string
    {
        $encoded = json_encode($value, JSON_UNESCAPED_SLASHES);
        if (!is_string($encoded)) {
            throw new \RuntimeException('json_encode_failed');
        }
        return $encoded;
    }

    private static function base64Url(string $value): string
    {
        return rtrim(strtr(base64_encode($value), '+/', '-_'), '=');
    }

    private static function safeLog(string $code): void
    {
        if (function_exists('AddMessage2Log')) {
            AddMessage2Log('[customer-settlements] ' . $code, 'mastermobile');
        }
    }
}
