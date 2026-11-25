<?php

function calculTva($montant, $codeTaux = 1, $depuisTtc = false)
{
    if ($codeTaux == 1) $taux = 0.20;
    elseif ($codeTaux == 2) $taux = 0.10;
    elseif ($codeTaux == 3) $taux = 0.055;
    elseif ($codeTaux == 4) $taux = 0.021;
    else $taux = 0;

    if (!$depuisTtc) {
        $ttc = $montant * (1 + $taux);
        $tva = $ttc - $montant;
        $ht = $montant;
    } else {
        $ht = $montant / (1 + $taux);
        $tva = $montant - $ht;
        $ttc = $montant;
    }

    return [
        'ht' => round($ht, 2),
        'tva' => round($tva, 2),
        'ttc' => round($ttc, 2)
    ];
}

function statsMarge($achatHt, $venteTtc, $tva = 20)
{
    if ($achatHt <= 0 || $venteTtc <= 0) {
        return false;
    }

    $venteHt = $venteTtc / (1 + ($tva / 100));
    $marge = $venteHt - $achatHt;

    return [
        'marge_ht' => round($marge, 2),
        'taux_marge' => round(($marge / $achatHt) * 100, 2),
        'taux_marque' => round(($marge / $venteHt) * 100, 2),
        'coeff' => round($venteTtc / $achatHt, 3)
    ];
}

function calculerFacture($lignes, $remiseGlobale = 0)
{
    $totalHt = 0;
    $tvaParTaux = [];
    $grilleTva = [1 => 0.20, 2 => 0.10, 3 => 0.055, 4 => 0.021];

    foreach ($lignes as $l) {
        $qte = $l['qte'];
        $pu = $l['pu'];
        $remLigne = isset($l['remise']) ? $l['remise'] : 0;
        $codeTva = isset($l['tva']) ? $l['tva'] : 1;

        $netLigne = $qte * $pu * (1 - ($remLigne / 100));
        $netLigne = $netLigne * (1 - ($remiseGlobale / 100));
        $totalHt += $netLigne;

        $tx = isset($grilleTva[$codeTva]) ? $grilleTva[$codeTva] : 0.20;
        $cle = (string) ($tx * 100);
        if (!isset($tvaParTaux[$cle])) {
            $tvaParTaux[$cle] = 0;
        }
        $tvaParTaux[$cle] += $netLigne * $tx;
    }

    $port = ($totalHt >= 150 || $totalHt == 0) ? 0 : 9.90;
    if ($port > 0) {
        if (!isset($tvaParTaux['20'])) $tvaParTaux['20'] = 0;
        $tvaParTaux['20'] += $port * 0.20;
    }

    $totalTva = 0;
    foreach ($tvaParTaux as $k => $v) {
        $tvaParTaux[$k] = round($v, 2);
        $totalTva += $tvaParTaux[$k];
    }

    return [
        'total_ht' => round($totalHt + $port, 2),
        'frais_port' => $port,
        'detail_tva' => $tvaParTaux,
        'total_tva' => round($totalTva, 2),
        'total_ttc' => round($totalHt + $port + $totalTva, 2)
    ];
}

function calculMensualite($capital, $tauxAnnuel, $annees)
{
    $nbMois = $annees * 12;
    $tauxMensuel = ($tauxAnnuel / 100) / 12;

    if ($tauxMensuel == 0) {
        $mensualite = $capital / $nbMois;
    } else {
        $mensualite = $capital * ($tauxMensuel / (1 - pow(1 + $tauxMensuel, -$nbMois)));
    }

    $totalRembourse = $mensualite * $nbMois;

    return [
        'mensualite' => round($mensualite, 2),
        'total' => round($totalRembourse, 2),
        'interets' => round($totalRembourse - $capital, 2)
    ];
}

function simulationEpargne($capitalInit, $versementMois, $tauxAnnuel, $annees, $avecFlatTax = true)
{
    $nbMois = $annees * 12;
    $tauxMensuel = ($tauxAnnuel / 100) / 12;
    $solde = $capitalInit;

    for ($i = 0; $i < $nbMois; $i++) {
        $solde = ($solde + $versementMois) * (1 + $tauxMensuel);
    }

    $totalVerse = $capitalInit + ($versementMois * $nbMois);
    $plusValue = $solde - $totalVerse;
    $impot = ($avecFlatTax && $plusValue > 0) ? $plusValue * 0.30 : 0;

    return [
        'verse' => round($totalVerse, 2),
        'brut' => round($solde, 2),
        'plus_value' => round($plusValue, 2),
        'impot' => round($impot, 2),
        'net' => round($solde - $impot, 2)
    ];
}
