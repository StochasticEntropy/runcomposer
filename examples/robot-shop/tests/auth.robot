*** Settings ***
Documentation     Authentication checks of the fictional web shop (neutral demo
...               corpus). Area tag `Auth`, sub-area tags `Auth-Login`,
...               `Auth-Session`.

*** Test Cases ***
Login With Valid Credentials
    [Tags]    Auth    Auth-Login    Smoke    Sprint-12    SHOP-1300
    Should Be Equal    signed-in    signed-in

Login With Wrong Password Fails
    [Tags]    Auth    Auth-Login    Regression    Sprint-12
    Should Contain    invalid credentials    invalid

Account Locks After Five Attempts
    [Tags]    Auth    Auth-Login    Regression    Sprint-13    SHOP-1303
    ${attempts}=    Evaluate    5
    Should Be True    ${attempts} >= 5

Password Reset Sends A Mail
    [Tags]    Auth    Auth-Login    Regression    Sprint-14
    Should Contain    reset link sent    reset

Session Survives A Page Reload
    [Tags]    Auth    Auth-Session    Regression    Sprint-13
    Should Be True    ${True}

Logout Clears The Session
    [Tags]    Auth    Auth-Session    Smoke    Sprint-13    SHOP-1311
    ${session}=    Set Variable    ${EMPTY}
    Should Be Empty    ${session}

Idle Session Expires After Thirty Minutes
    [Tags]    Auth    Auth-Session    Regression    Sprint-14
    ${minutes}=    Evaluate    30
    Should Be Equal As Integers    ${minutes}    30

Remember Me Extends The Session
    [Tags]    Auth    Auth-Session    Regression    Sprint-14    Quarantine-Flaky
    [Documentation]    Quarantined: timing-dependent in the fictional shop.
    Should Be True    ${True}
