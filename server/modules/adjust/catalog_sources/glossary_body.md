[Datascape](/en/article/datascape) lets you pull aggregated data from multiple sources into one report using the [Report Service API](https://dev.adjust.com/en/api/rs-api/). This means you can retrieve data from Adjust KPI Service, SKAdNetwork, and Ad Spend.

+++>
### About the API Metric ID

You can use the API Metric ID as the `metrics` parameter when using the [Report Service API](https://dev.adjust.com/en/api/rs-api/) to query and retrieve your data from Adjust.

===
### Find the event_slug

The Report Service API uses actual event names, rather than tokens, to fetch event information. To find the correct event slug for your chosen event, you can use the [Events endpoint](https://dev.adjust.com/en/api/rs-api/events/).

===
### Reporting for ROAS and ROI metrics

In Datascape all ROAS and ROI metrics are displayed using percentage values. For example: `40%`. 

However, these metrics report as a decimal value when requested from the Report Service API. For example: `0.4`.

+++<

## [Conversion metrics](conversion-metrics)

These metrics help you measure your user activity and converstion rates. 

<table>
  <thead>
    <tr>
      <th></th>
      <th>Definition</th>
      <th>Formula</th>
      <th>API Metric ID</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <th>ATT - Authorized Users</th>
      <td>The number of users with an authorized ATT status.</td>
      <td>-</td>
      <td>att_status_authorized</td>
    </tr>
    <tr>
      <th>ATT - Not Determined Users</th>
      <td>The number of users with a not determined ATT status.</td>
      <td>-</td>
      <td>att_status_non_determined</td>
    </tr>
    <tr>
      <th>ATT - Denied Users</th>
      <td>The number of users with a denied ATT status.</td>
      <td>-</td>
      <td>att_status_denied</td>
    </tr>
    <tr>
      <th>ATT - Restricted Users</th>
      <td>The number of users with a restricted ATT status.</td>
      <td>-</td>
      <td>att_status_restricted</td>
    </tr>
    <tr>
      <th>ATT Consent Rate</th>
      <td>The percentage of users who were shown, and consented to, the ATT prompt.</td>
      <td><code>ATT Status Authorized</code> / ( <code>ATT Status Denied</code> + <code>ATT Status Authorized</code> )</td>
      <td>att_consent_rate</td>
    </tr>
    <tr>
      <th>Avg. DAUs</th>
      <td>The average number of unique daily active users (DAU) for your selected timeframe.</td>
      <td>(D0 DAU + D1 DAU + DAY N DAU) / the number of days in your timeframe</td>
      <td>daus</td>
    </tr>
    <tr>
      <th>Avg. MAUs</th>
      <td>The average number of unique monthly active users (MAU) for your selected timeframe.</td>
      <td>(D0 MAU + D1 MAU + DAY N MAU) / the number of months in your timeframe</td>
      <td>maus</td>
    </tr>
    <tr>
      <th>Avg. WAUs</th>
      <td>The average number of unique weekly active users (WAU) for your selected timeframe.</td>
      <td>(D0 WAU + D1 WAU + DAY N WAU) / the number of weeks in your timeframe</td>
      <td>waus</td>
    </tr>
    <tr>
      <th>Base Sessions</th>
      <td>The number of user sessions, excluding installs and reattributions.</td>
      <td>-</td>
      <td>base_sessions</td>
    </tr>
    <tr>
      <th>Clicks</th>
      <td>For SANs: the number of clicks we received from the network.<br />For non-SANs: the number of clicks we measured directly.</td>
      <td>-</td>
      <td>clicks</td>
    </tr>
    <tr>
      <th>Clicks (Attribution)</th>
      <td>The total number of clicks measured for your campaigns.</td>
      <td>-</td>
      <td>attribution_clicks</td>
    </tr>
    <tr>
      <th>Clicks (Network)</th>
      <td>The number of clicks reported by the network.</td>
      <td>-</td>
      <td>network_clicks</td>
    </tr>
    <tr>
      <th>Click Conversion Rate (CCR)</th>
      <td>The average number of clicks it takes for a user to install your app.</td>
      <td><code>Installs</code>/ <code>Clicks</code> *100</td>
      <td>click_conversion_rate</td>
    </tr>
    <tr>
      <th>Click Through Rate (CTR)&nbsp;</th>
      <td>The percentage of clicks you received per impressions served.</td>
      <td><code>Clicks</code> / <code>Impressions</code>&nbsp;*100</td>
      <td>ctr</td>
    </tr>
    <tr>
      <th>Deattributions</th>
      <td>The total number of users that are removed away from the first attribution source to a reattribution source.</td>
      <td>-</td>
      <td>deattributions</td>
    </tr>
    <tr>
      <th>Event</th>
      <td>Non-cohorted&nbsp;number of times a specific event is triggered per period.</td>
      <td>-</td>
      <td>{event_slug}_events</td>
    </tr>
    <tr>
      <th>Total Events</th>
      <td>The total number of all triggered events.</td>
      <td>-</td>
      <td>events</td>
    </tr>
    <tr>
      <th>First Reinstalls</th>
      <td>The number of first time reinstalls per period of time.&nbsp;Only available with Uninstall and Reinstall Growth Solution.</td>
      <td>-</td>
      <td>first_reinstalls</td>
    </tr>
    <tr>
      <th>First Uninstalls</th>
      <td>The number of first time uninstalls per period of time.&nbsp;Only available with Uninstall and Reinstall Growth Solution.</td>
      <td>-</td>
      <td>first_uninstalls</td>
    </tr>
    <tr>
      <th>GDPR Forgets</th>
      <td>The total number of users who have exercised their right to be forgotten under the EU's GDPR. Adjust permanently deletes the historical personal data for these users but retains their aggregated data for reports.</td>
      <td>-</td>
      <td>gdpr_forgets</td>
    </tr>
    <tr>
      <th>Impressions</th>
      <td>For SANs: the number of impressions we received from the network. <br />For non-SANs: the number of impressions we measured directly.</td>
      <td>-</td>
      <td>impressions</td>
    </tr>
    <tr>
      <th>Impressions (Attribution)</th>
      <td>The total number of ad impressions measured for your campaigns.</td>
      <td>-</td>
      <td>attribution_impressions</td>
    </tr>
    <tr>
      <th>Impressions (Network)</th>
      <td>Number of impressions reported by the network.</td>
      <td>-</td>
      <td>network_impressions</td>
    </tr>
    <tr>
      <th>Impression Conversion Rate (ICR)</th>
      <td>The percentage of app installs per ad impressions served.</td>
      <td><code>Installs</code>&nbsp;/&nbsp;<code>Impressions</code>&nbsp;*100</td>
      <td>impression_conversion_rate</td>
    </tr>
    <tr>
      <th>Installs</th>
      <td>The number of installs for your app.</td>
      <td>-</td>
      <td>installs</td>
    </tr>
    <tr>
      <th>Installs (Network)</th>
      <td>The number of installs reported by the network.</td>
      <td>-</td>
      <td>network_installs</td>
    </tr>
    <tr>
      <th>Installs Diff (Network)</th>
      <td>Shows an absolute value with the difference between the Network and Attribution sources.</td>
      <td>| <code>Network Installs</code> -&nbsp;<code>Installs</code> |</td>
      <td>network_installs_diff</td>
    </tr>
    <tr>
      <th>Installs Diff (Network) (Signed)</th>
      <td>The installs value difference between the Network and Attribution sources. Value can be negative if there are more attribution installs than network installs.</td>
      <td><code>Network installs</code> -&nbsp;<code>Installs</code></td>
      <td>network_installs_diff_signed</td>
    </tr>
    <tr>
      <th>Installs per Mile (IPM)</th>
      <td>Installs per one thousand impressions.</td>
      <td>1000 * <code>Impression conversion rate</code></td>
      <td>installs_per_mile</td>
    </tr>
    <tr>
      <th>Limit Ad Tracking Installs</th>
      <td>The total number of installs coming from devices with limit ad tracking (LAT) enabled.</td>
      <td>-</td>
      <td>limit_ad_tracking_installs</td>
    </tr>
    <tr>
      <th>Limit Ad Tracking Rate</th>
      <td>The percentage of your total installs coming from devices with LAT enabled.</td>
      <td><code>Limit ad tracking installs</code>/ <code>Installs</code></td>
      <td>limit_ad_tracking_install_rate</td>
    </tr>
    <tr>
      <th>Limit Ad Tracking Reattributions</th>
      <td>The total number of reattributions coming from devices with LAT enabled.</td>
      <td>-</td>
      <td>limit_ad_tracking_reattributions</td>
    </tr>
    <tr>
      <th>Limit Ad Tracking Reattribution Rate</th>
      <td>The percentage of your total reattributions coming from devices with LAT enabled.&nbsp;</td>
      <td><code>Limit ad tracking reattributions</code> / <code>Reattributions</code></td>
      <td>limit_ad_tracking_reattribution_rate</td>
    </tr>
    <tr>
      <th>Non-Organic Installs</th>
      <td>The number of Installs that are not attributed to an Organic source.</td>
      <td>-</td>
      <td>non_organic_installs</td>
    </tr>
    <tr>
      <th>Organic Installs</th>
      <td>The number of Installs that are attributed to an Organic source.</td>
      <td>-</td>
      <td>organic_installs</td>
    </tr>
    <tr>
      <th>Reattribution</th>
      <td>The total number of reattributions that have occurred. <a href="/en/article/reattribution">See reattribution measurement</a>.</td>
      <td>-</td>
      <td>reattributions</td>
    </tr>
    <tr>
      <th>Reattribution Reinstalls</th>
      <td>The total number of reinstalls that occurred that also led to a reattribution.</td>
      <td>-</td>
      <td>reattribution_reinstalls</td>
    </tr>
    <tr>
      <th>Redownload installs</th>
      <td>Count of redownload installs, reported on the new attribution source.  Redownload installs are included in the main Installs metric.</td>
      <td>-</td>
      <td>redownload_installs</td>
    </tr>
    <tr>
      <th>Redownload deinstalls</th>
      <td>Count of redownload installs, reported on the previous attribution source(s) before the redownload attribution took place.</td>
      <td>-</td>
      <td>redownload_deinstalls</td>
    </tr>
    <tr>
      <th>Redownload reattributions</th>
      <td>Count of Reattributions that took place when processing the redownload session, that didn’t qualify to be an install. Redownload reattributions are included in the main Reattributions metric.</td>
      <td>-</td>
      <td>redownload_reattributions</td>
    </tr>
    <tr>
       <th>Redownload sessions</th>
      <td>Count of all redownload sessions received for the app. Similar to the main Sessions metric, Redownload sessions include redownload installs and redownload reattributions.</td>
      <td>-</td>
      <td>redownload_sessions</td>
    </tr>   
    <tr>
      <th>Reinstalls</th>
      <td>The total number of reinstalls that have occurred.&nbsp;Only available with&nbsp;Uninstall and Reinstall Growth Solution.</td>
      <td>-</td>
      <td>reinstalls</td>
    </tr>
    <tr>
      <th>Renewals</th>
      <td>Renewals</td>
      <td></td>
      <td>renewals</td>
    </tr>
    <tr>
      <th>Sessions</th>
      <td>The total number of sessions, including installs (first sessions), reinstalls, reattributions, and reattribution reinstalls that have occurred.</td>
      <td><code>base_sessions</code> + <code>installs</code> + <code>reattributions</code></td>
      <td>sessions</td>
    </tr>
    <tr>
      <th>Uninstalls&nbsp;</th>
      <td>Number of uninstalls. Only available with&nbsp;<a href="/en/article/uninstalls-reinstalls">Uninstall and Reinstall Growth Solution.</a></td>
      <td>-</td>
      <td>uninstalls</td>
    </tr>
    <tr>
      <th>Uninstalls (Cohort)</th>
      <td>The total number of uninstalls from users who installed your app within your selected timeframe.&nbsp;Only available with&nbsp;Uninstall and Reinstall Growth Solution.</td>
      <td>-</td>
      <td>uninstall_cohort</td>
    </tr>
  </tbody>
</table>


## [Cohort metrics](cohort-metrics)

These metrics help you measure cohorted data for users who installed or were reattributed to your app. `N days` is a placeholder for your actual cohort period. 

+++>
### [Available cohort periods](available-cohort-periods)

Adjust supports the following cohort periods:

$$$>

__Days:__

- 0D - 120D

===

__Weeks:__

- 0W - 52W

===

__Months:__

- 0M - 36M

$$$<

+++<

Not all cohort days are selectable in Datascape. You can use the [Report Service API](https://dev.adjust.com/en/api/rs-api/) to view cohort data for any specfic day between 0D - 120D.

<callout type="tip">
Alongside providing both cumulative and non-cumulative metrics, Adjust offers metrics that are calculated using different definitions of the cohort group. <a href="/en/article/how-cohorts-work">Find out what these are and how cohorts work</a>.
</callout>

### [Cumulative cohort metrics](cumulative-cohort-metrics)

<table>
  <thead>
    <tr>
      <th></th>
      <th>Definition</th>
      <th>Formula</th>
      <th>API Metric ID</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <th>N days Ad Impressions Total</th>
      <td>The cumulative number of ads served to users who installed or were reattributed to the app within the selected date range over the selected cohort period.</td>
      <td>-</td>
      <td>ad_impressions_total_{cohort_period}</td>
    </tr>
     <tr>
      <th>N days Cost Per First-Time Paying User Total</th>
      <td>The average ad spend per cumulative count of users who completed their first in-app purchase by the selected cohort period.</td>
      <td><code>Ad Spend / First Time Paying Users Total N days</code></td>
      <td>cost_per_paying_user_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Ad Impressions Total in Cohort</th>
      <td>The cumulative number of ads served to users who installed or were reattributed to the app within the selected date range over the selected cohort period.</td>
      <td>-</td>
      <td>ad_impressions_total_in_cohort_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Event (Conversions)</th>
      <td>Number of users who completed the relevant in-app event on day, week, or month <b>N</b> after install or reattribution.</td>
      <td></td>
      <td>{event_slug}_{cohort_period}_conversions_cohort</td>
    </tr>
    <tr>
      <th>N days Event (Events)</th>
      <td>Number of events completed on day, week, or month <b>N</b> after install.</td>
      <td></td>
      <td>{event_slug}_{cohort_period}_events_cohort</td>
    </tr>
    <tr>
      <th>N days Event (Revenue)</th>
      <td>Amount of in-app revenue, based on revenue events reported by the Adjust SDK or recorded server-to-server.</td>
      <td></td>
      <td>{event_slug}_{cohort_period}_revenue_cohort</td>
    </tr>
    <tr>
      <th>N days Event (Converted User Size)</th>
      <td>Number of users who completed the relevant in-app event by day, week or month N and installed your app at least N days, weeks or months ago.</td>
      <td></td>
      <td>{event_slug}_{cohort_period}_converted_user_size_cohort</td>
    </tr>
    <tr>
      <th>N days Events per Conversion (Events)</th>
      <td>Number of triggered events divided by the number of conversions.</td>
      <td><code>{event slug} {cohort period} events cohort / {event slug} {cohort period} conversions cohort</code></td>
      <td>{event_slug}_{cohort_period}_events_per_conversion_cohort</td>
    </tr>
    <tr>
      <th>N days Events per Conversion (Revenue)</th>
      <td>Revenue generated by the chosen event divided by the number of conversions.</td>
      <td><code>{event slug} {cohort period} revenue cohort / {event slug} {cohort period} conversions cohort</code></td>
      <td>{event_slug}_{cohort_period}_revenue_per_conversion_cohort</td>
    </tr>
    <tr>
      <th>N days Event ( Event Rate)&nbsp;</th>
      <td>The number of times the chosen event occurred, divided by the cohort size.<br />The event rate is calculated by taking the total occurrences of your selected event and dividing it by the number of people in the cohort.</td>
      <td><code>{event slug} {cohort period} events cohort / cohort size</code></td>
      <td>{event_slug}_{cohort_period}_events_rate_cohort</td>
    </tr>
    <tr>
      <th>N days Event (Conversions Rate)</th>
      <td>The number of times a chosen event was triggered for the first time, divided by the cohort size. <br />The conversion rate is calculated by taking the total number of conversions and dividing it by the number of people in the cohort.</td>
      <td><code>{event slug} {cohort period} conversions cohort / cohort size</code></td>
      <td>{event_slug}_{cohort_period}_conversions_rate_cohort</td>
    </tr>
    <tr>
      <th>N days Revenue Total</th>
      <td>The cumulative amount of in-app revenue by users who installed or were reattributed to the app within the selected date range over the selected cohort period.</td>
      <td>-</td>
      <td>revenue_total_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Revenue Total Per User</th>
      <td>The cumulative in-app revenue per user for a selected cohort period.</td>
      <td><code>Revenue total N days</code>&nbsp;/&nbsp;<code>Cohort size N days</code></td>
      <td>revenue_total_per_user_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Revenue Total Per Paying User</th>
      <td>Cumulative in-app revenue per paying user within the selected cohort period.</td>
      <td><code>N days Revenue Total / N days First Paying Users Total</code></td>
      <td>revenue_total_per_paying_user_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Revenue Total In Cohort</th>
      <td>(Number of days, weeks, or months) Revenue Total In Cohort</td>
      <td>-</td>
      <td>revenue_total_in_cohort_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Revenue Events Total&nbsp;</th>
      <td>(Number of days, weeks, or months) Revenue Events Total</td>
      <td>-</td>
      <td>revenue_events_total_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Revenue Events Total in Cohort</th>
      <td>The cumulative number of revenue events generated during the selected cohort period. Only users who have completed the entire cohort period after install are included in the calculation.&nbsp;</td>
      <td>-</td>
      <td>revenue_events_total_in_cohort_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Revenue Events Total per Paying User</th>
      <td>Cumulative number of revenue-generating events per paying user within the selected cohort period.</td>
      <td><code>N days revenue events total in cohort / N days first paying users total</code></td>
      <td>revenue_events_total_per_paying_user_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Ad Revenue Total</th>
      <td>The cumulative amount of revenue generated by serving in-app ads to users who installed or were reattributed to the app within the selected date range over the selected cohort period.</td>
      <td>-</td>
      <td>ad_revenue_total_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Ad Revenue Total Per User</th>
      <td>The cumulative ad revenue per user for a selected cohort period.</td>
      <td><code>Ad revenue total N days&nbsp;</code>/&nbsp;<code>Cohort size N days</code></td>
      <td>ad_revenue_total_per_user_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Ad Revenue Total in Cohort</th>
      <td>The total ad revenue on day N from all the users in the N day cohort who fully completed the Nth day.&nbsp;</td>
      <td>-</td>
      <td>ad_revenue_total_in_cohort_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days All Revenue Total</th>
      <td>The total cumulative revenue on day N from all the users in the 0 day cohort size. Users do not have to reach day N.</td>
      <td><code>Ad revenue total N days&nbsp;</code>+&nbsp;<code>Revenue total N days</code></td>
      <td>all_revenue_total_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days All Revenue Total Per User</th>
      <td>The cumulative revenue from all revenue sources per user for a selected cohort period.</td>
      <td><code>All revenue total N days</code>&nbsp;/&nbsp;<code>Cohort size N days</code></td>
      <td>all_revenue_total_per_user_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days All Revenue Total in Cohort</th>
      <td>Total revenue on day N from all the users in the N day cohort. Only includes users who fully completed the N day.</td>
      <td><code>Ad revenue total in cohort N days + Revenue total in cohort N days</code></td>
      <td>all_revenue_total_in_cohort_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days LTV (Ad) All Users</th>
      <td>The lifetime value of a user for a specified cohort period, calculated using only revenue from serving ads.</td>
      <td><code>Ad revenue total in cohort N days</code> / <code>Cohort size N days</code></td>
      <td>lifetime_value_ad_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days LTV (Ad) Paying Users</th>
      <td>The lifetime value of a paying user for a specified cohort period, calculated using only revenue from serving ads.</td>
      <td><code>Ad revenue total in cohort N days</code> / <code>Paying user size N days</code></td>
      <td>paying_user_lifetime_value_ad_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days LTV (All) All Users</th>
      <td>The lifetime value of a user, calculated using all revenue sources for a specified cohort period.</td>
      <td><code>All revenue total in cohort N days</code> / <code>Cohort size N days</code></td>
      <td>lifetime_value_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days ROAS (Ad Revenue)</th>
      <td>Return on advertising spend, calculated using only revenue from serving ads, for a specified cohort period.</td>
      <td><code>Ad revenue total N days</code>&nbsp;/ <code>Ad Spend</code></td>
      <td>roas_ad_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days ROAS (All Revenue)</th>
      <td>Return on advertising spend, calculated using all revenue sources for a specified cohort period.</td>
      <td><code>All revenue total N days</code> / <code>Ad Spend</code></td>
      <td>roas_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days ROAS (IAP Revenue)</th>
      <td>Return on advertising spend, calculated using only in-app revenue, for a selected cohort period.</td>
      <td><code>Revenue total N days</code>&nbsp;/ <code>Ad Spend</code></td>
      <td>roas_iap_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days LTV (All) Paying Users</th>
      <td>The lifetime value of a paying user, calculated using all revenue sources for a specified cohort period.</td>
      <td><code>All revenue total in cohort N days</code> / <code>Paying user size N days</code></td>
      <td>paying_user_lifetime_value_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days LTV (IAP) All Users</th>
      <td>User lifetime value, calculated using only in-app revenue, for a selected cohort period.</td>
      <td><code>Revenue total in cohort N days</code> / <code>Cohort size N days</code></td>
      <td>lifetime_value_iap_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days LTV (IAP) Paying Users</th>
      <td>The lifetime value for paying users, calculated using only in-app revenue, for a selected cohort period.</td>
      <td><code>Revenue total in cohort N days</code> / <code>Paying user size N days</code></td>
      <td>paying_user_lifetime_value_iap_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Total First-time Paying Users</th>
      <td>The cumulative count of users who completed their first in-app purchase by the selected cohort period.</td>
      <td>-</td>
      <td>first_paying_users_total_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days First-Time Paying Users Conversion Rate Total</th>
      <td>The share of cumulative count of users who completed their first in-app purchase by the selected cohort period.</td>
      <td><code>First Time Paying Users Total N days / Cohort size N days</code></td>
      <td>cumulative_paying_users_conversion_rate_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days First Reinstalls Total</th>
      <td>The total cumulative number of first-time reinstalls by users who installed the app in a selected date range and have had the app installed for a selected cohort period.</td>
      <td>-</td>
      <td>first_reinstalls_total_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Reinstalls Total</th>
      <td>The cumulative number of users who installed the app in your selected timeframe and also have reinstalled it during a specified cohort period.</td>
      <td>-</td>
      <td>reinstalls_total_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days First Uninstalls Total</th>
      <td>The total cumulative number of first-time uninstalls by users who installed the app in a selected date range and have had the app installed for a selected cohort period.</td>
      <td>-</td>
      <td>first_uninstalls_total_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Uninstalls Total</th>
      <td>The cumulative number of users who installed or reinstalled the app in your selected timeframe and also have uninstalled it during a specified cohort period.</td>
      <td>-</td>
      <td>uninstalls_total_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days GDPR Forget Users Total</th>
      <td>The cumulative number of GDPR Forgets by users who installed or were reattributed to the app within the selected date range over the selected cohort period.</td>
      <td>-</td>
      <td>gdpr_forgets_total_{cohort_period}</td>
    </tr>
  </tbody>
</table>


### [Non-cumulative cohort metrics](non-cumulative-cohort-metrics)

<table>
  <thead>
    <tr>
      <th></th>
      <th>Definition</th>
      <th>Formula</th>
      <th>API Metric ID</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <th>N days Ad Impressions</th>
      <td>The number of ads served to users who installed or were reattributed to the app within the selected date range over the selected cohort period.</td>
      <td>-</td>
      <td>ad_impressions_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Ad Revenue</th>
      <td>The amount of ad revenue generated in a selected date range for a selected cohort period.</td>
      <td>-</td>
      <td>ad_revenue_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Ad RPM</th>
      <td>(Number of days, weeks, or months) Ad RPM</td>
      <td>(<code>Ad revenue N days </code>/ <code>Ad impressions N days</code>) * 1000</td>
      <td>ad_rpm_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days All Revenue</th>
      <td>The total amount of revenue generated by users who installed or were reattributed to the app within the selected date range over the selected cohort period.</td>
      <td>-</td>
      <td>all_revenue_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days All Revenue Per User</th>
      <td>Revenue per user, calculated using all revenue sources for a specified cohort period.</td>
      <td><code>All revenue N days</code> / <code>Cohort size N days</code></td>
      <td>all_revenue_per_user_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Cohort Size</th>
      <td>The number of users who were attributed to the given source (first install or last source) for at least N days, weeks or months, within your selected timeframe. </td>
      <td>-</td>
      <td>cohort_size_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Deattributions</th>
      <td>The number of users who installed or were reattributed to the app within the selected timeframe over the specified cohort period.</td>
      <td>-</td>
      <td>deattributions_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Cost per Event (Events)</th>
      <td>Ad spend divided by the number of triggered events.</td>
      <td><code>Ad Spend / {event slug} {cohort period} events cohort</code></td>
      <td>{event_slug}_{cohort_period}_events_cost_cohort</td>
    </tr>
    <tr>
      <th>N days Cost per Event (Conversions)</th>
      <td>Ad spend divided by the number of conversions.</td>
      <td><code>Ad Spend / {event slug} {cohort period} conversions cohort</code></td>
      <td>{event_slug}_{cohort_period}_conversions_cost_cohort</td>
    </tr>
    <tr>
      <th>N days Deattributions Per User</th>
      <td>The number of deattributions per user for selected cohort period.</td>
      <td><code>Deattributions N days</code> / <code>Cohort size N days</code></td>
      <td>deattributions_per_user_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Event (Events per Period)</th>
      <td>The number of times an event was triggered during the cohort period.</td>
      <td>-</td>
      <td>{event_slug}_{cohort_period}_events_per_period</td>
    </tr>
    <tr>
      <th>N days Event (Revenue per Period)</th>
      <td>Amount of in-app revenue earned from the event, as reported by the Adjust SDK or recorded server-to-server, within the selected cohort period.</td>
      <td>-</td>
      <td>{event_slug}_{cohort_period}_revenue_per_period</td>
    </tr>
    <tr>
      <th>N days First Reinstalls</th>
      <td>The total number of first-time reinstalls by users who installed the app in a selected date range and have had the app installed for a selected cohort period.</td>
      <td>-</td>
      <td>first_reinstalls_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days First Uninstalls</th>
      <td>The total number of first-time uninstalls by users who installed the app in a selected date range and have had the app installed for a selected cohort period.</td>
      <td>-</td>
      <td>first_uninstalls_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days GDPR Forgets</th>
      <td>The number of GDPR Forgets by users who installed or were reattributed to the app within the selected date range over the selected cohort period.</td>
      <td>-</td>
      <td>gdpr_forgets_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Non-Install Sessions</th>
      <td>The total number of non-install sessions by users who installed or were reattributed to the app within the selected date range over the selected cohort period.</td>
      <td>-</td>
      <td>non_install_sessions_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days First-Time Paying Users</th>
      <td>The count of users who completed their first in-app purchase during the selected cohort period.</td>
      <td>-</td>
      <td>first_paying_users_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days First-Time Paying Users Conversion Rate (Retained Users)</th>
      <td>The count of users who completed their first in-app purchase during the selected cohort period divided by the count of retained users in that cohort period.</td>
      <td><code>First Time Paying Users N days / Retained users N days</code></td>
      <td>first_time_paying_user_conversion_rate_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Paying User Size</th>
      <td>The total number of users who installed or were reattributed in your selected timeframe, have had your app installed and/or were reattributed for the duration of a specified cohort period and completed an in-app purchase at any point.</td>
      <td>-</td>
      <td>paying_user_size_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days First-Time Paying Users Conversion Rate</th>
      <td>The share of users who completed their first in-app purchase during the selected cohort period.</td>
      <td><code>First Time Paying Users N days / Cohort size N days</code></td>
      <td>paying_user_conversion_rate_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Paying Users</th>
      <td>The count of users who completed an in-app purchase during the selected cohort period.</td>
      <td>-</td>
      <td>paying_users_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Paying Users Rate</th>
      <td>The share of paying users in a cohort for a specified cohort period.</td>
      <td><code>Paying users N days</code> / <code>Cohort size N days</code></td>
      <td>paying_user_rate_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Reattributions</th>
      <td>The number of users who installed or were reattributed to the app within the selected timeframe and were reattributed during a specified cohort period.</td>
      <td>-</td>
      <td>reattributions_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Reattributions per Deattribution</th>
      <td>The number of reattributions divided by number of deattributions for selected cohort period.</td>
      <td><code>Reattributions N days</code> / <code>Deattributions N days</code></td>
      <td>reattributions_per_deattribution_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Reattributions Per User</th>
      <td>The number of reattributions per user of deattributions for selected cohort period.</td>
      <td><code>Reattributions N days</code> / <code>Cohort size N days</code></td>
      <td>reattributions_per_user_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Reinstalls</th>
      <td>The number of users who installed the app in your selected timeframe and also have reinstalled it during a specified cohort period.</td>
      <td>-</td>
      <td>reinstalls_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Retained Users</th>
      <td>The number of users who installed or were reattributed to the app within the selected time frame and stayed active for a duration specified in a cohort period.</td>
      <td>-</td>
      <td>retained_users_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Retention Rate All Users</th>
      <td>The number of retained users divided by a cohort size.</td>
      <td><code>Retained users N days</code> / <code>Cohort size N days</code></td>
      <td>retention_rate_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Retention Rate Paying Users</th>
      <td>(Number of days, weeks, or months) Retention rate of paying users.</td>
      <td><code>Paying users N days</code> / <code>Retained users N days</code></td>
      <td>paying_users_retention_rate_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Revenue</th>
      <td>Amount of in-app revenue by users who installed or were reattributed to the app within the selected date range over the selected cohort period.</td>
      <td>-</td>
      <td>revenue_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Revenue Events</th>
      <td>The number of revenue events that have been triggered by users who installed or were reattributed to the app within the selected date range over the selected cohort period.</td>
      <td>-</td>
      <td>revenue_events_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Revenue Events Per Active User</th>
      <td>Number of revenue events divided by the number of retained users for a selected cohort period.</td>
      <td><code>Revenue events N days </code>/ <code>Retained users N days</code></td>
      <td>revenue_events_per_active_user_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Revenue Events Per Paying User</th>
      <td>Number of revenue events divided by the number of paying users for a selected cohort period.</td>
      <td><code>Revenue events N days</code> / <code>Paying users N days</code></td>
      <td>revenue_events_per_paying_user_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Revenue Events Per User</th>
      <td>The number of revenue events divided by a cohort size for a selected cohort period.</td>
      <td><code>Revenue events N days</code> / <code>Cohort size N days</code></td>
      <td>revenue_events_per_user_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Revenue Per Paying User</th>
      <td>Average in-app revenue per paying user for a selected cohort period.</td>
      <td><code>Revenue N days</code> / <code>Paying users N days</code></td>
      <td>revenue_per_paying_user_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Revenue Per User</th>
      <td>Average in-app revenue per user for a selected cohort period.</td>
      <td><code>Revenue N days</code> / <code>Cohort size N days</code></td>
      <td>revenue_per_user_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Sessions</th>
      <td>The number of sessions by users who installed or were reattributed to the app within the selected date range over the selected cohort period.</td>
      <td>-</td>
      <td>sessions_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Sessions Per User</th>
      <td>The average number of sessions per user during selected cohort period.</td>
      <td><code>Sessions N days</code> / <code>Cohort size N days</code></td>
      <td>sessions_per_user_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Time Spent</th>
      <td>The number of seconds spent in the app by users who installed or were reattributed to the app wothin the selected date range over the selected cohort period.</td>
      <td>-</td>
      <td>time_spent_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Time Spent Per Active User</th>
      <td>The average number of seconds each active user in a cohort spent in the app during selected cohort period.</td>
      <td><code>Time spent N days</code> / <code>Retained users N days</code></td>
      <td>time_spent_per_active_user_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Time Spent Per Session</th>
      <td>The average number of seconds each session took during selected cohort period.</td>
      <td><code>Time spent N days</code> / <code>Non install sessions N days</code></td>
      <td>time_spent_per_session_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Time Spent Per User</th>
      <td>The average number of seconds each user in a cohort spent in the app during selected cohort period.</td>
      <td><code>Time spent N days</code> / <code>Cohort size N days</code></td>
      <td>time_spent_per_user_{cohort_period}</td>
    </tr>
    <tr>
      <th>N days Uninstalls</th>
      <td>The number of users who installed and/or reinstalled the app in your selected timeframe and also have uninstalled it during a specified cohort period.</td>
      <td>-</td>
      <td>uninstalls_{cohort_period}</td>
    </tr>
  </tbody>
</table>


## [Ad Spend metrics](ad-spend-metrics)

These help you measure your ad spend campaigns and identify key trends. Read more about [Adjust Ad Spend](/en/article/spendworks-adjust-ad-spend-solution). 

Metric calculations with `Ad Spend` in the formula depend on your selected ad spend source. Read more on [how ad spend source affects your data](/en/article/how-ad-spend-source-affects-your-data).

<table>
  <thead>
    <tr>
      <th></th>
      <th>Definition</th>
      <th>Formula</th>
      <th>API Metric ID</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <th>Ad Spend</th>
      <td>The amount of money spent on ads.</td>
      <td>The sum of <code>click_cost </code>+ <code>impression_cost </code>+ <code>install_cost</code>&nbsp;+ <code>event_cost</code></td>
      <td>cost</td>
    </tr>
    <tr>
      <th>Ad Spend (Attribution)</th>
      <td>The amount of money spent on ads. Calculated using only data retrieved using Adjust’s ad spend on engagement method.</td>
      <td>The sum of <code>click_cost</code> + <code>impression_cost</code> + <code>install_cost&nbsp;</code>+ <code>event_cost</code></td>
      <td>adjust_cost</td>
    </tr>
    <tr>
      <th>Ad Spend (Network)</th>
      <td>Shows ad spend data retrieved using the Network API.</td>
      <td>The sum of <code>click_cost</code> + <code>impression_cost</code> + <code>install_cost</code>+ <code>event_cost</code></td>
      <td>network_cost</td>
    </tr>
    <tr>
      <th>Ad Spend Diff (Network)</th>
      <td>Shows an absolute value with the difference between the Attribution and Network sources.</td>
      <td><code>Ad Spend (Attribution)&nbsp;</code>- <code>Ad Spend (Network)</code></td>
      <td>network_cost_diff</td>
    </tr>
    <tr>
      <th>Click Cost</th>
      <td>The costs of clicks.</td>
      <td>-</td>
      <td>click_cost</td>
    </tr>
    <tr>
      <th>Clicks (Paid)</th>
      <td>The number of clicks, for which there is cost data.</td>
      <td>-</td>
      <td>paid_clicks</td>
    </tr>
    <tr>
      <th>eCPI (All Installs)</th>
      <td>Effective cost per install on all installs.</td>
      <td><code>Ad Spend</code>/ <code>Installs</code></td>
      <td>ecpi_all</td>
    </tr>
    <tr>
      <th>eCPI (Network)&nbsp;</th>
      <td>Effective cost per install reported by the Network API.</td>
      <td><code>Ad Spend (Network)</code> / <code>Network installs</code></td>
      <td>network_ecpi</td>
    </tr>
    <tr>
      <th>eCPI (Paid Installs)&nbsp;</th>
      <td>Effect cost per install on paid installs.</td>
      <td><code>Ad Spend (Network)</code> / <code>Paid installs</code></td>
      <td>ecpi</td>
    </tr>
    <tr>
      <th>eCPI (SKAdNetwork)</th>
      <td>Effective cost per install on all installs through SKAdNetwork.</td>
      <td><code>Ad Spend (Network - SKAdNetwork) / Installs (SKAdNetwork)</code></td>
      <td>skad_ecpi</td>
    </tr>
    <tr>
      <th>eCPM (Attribution)&nbsp;</th>
      <td>Effective cost per mille (one thousand impressions) reported by the Attribution ad spend source.</td>
      <td>(<code>Ad Spend</code> / <code>Paid impressions</code>) * 1000</td>
      <td>ecpm</td>
    </tr>
    <tr>
      <th>eCPM (Network)&nbsp;</th>
      <td>Effective cost per mille (one thousand impressions) reported by the Network API.</td>
      <td>(<code>Ad Spend (Network)</code>/ <code>Network impressions</code>) * 1000</td>
      <td>network_ecpm</td>
    </tr>
    <tr>
      <th>eCPC</th>
      <td>Effective cost per click.</td>
      <td><code>Ad spend</code> / <code>Paid clicks</code></td>
      <td>ecpc</td>
    </tr>
    <tr>
      <th>Event cost</th>
      <td>The costs of events</td>
      <td>-</td>
      <td>event_cost</td>
    </tr>
    <tr>
      <th>Impression cost</th>
      <td>Cost of impressions.</td>
      <td>-</td>
      <td>impression_cost</td>
    </tr>
    <tr>
      <th>Impressions (Paid)</th>
      <td>The number of impressions for which there is ad spend data.</td>
      <td>-</td>
      <td>paid_impressions</td>
    </tr>
    <tr>
      <th>Install Cost</th>
      <td>The costs of installs.</td>
      <td>-</td>
      <td>install_cost</td>
    </tr>
    <tr>
      <th>Installs (Paid)</th>
      <td>The number of installs, for which there is ad spend data.</td>
      <td>-</td>
      <td>paid_installs</td>
    </tr>
  </tbody>
</table>

## [Revenue metrics](revenue-metrics)

These let you measure your ad revenue and user spending metrics. Read more about [Adjust Ad Revenue](/en/article/ad-revenue). 

<table>
  <thead>
    <tr>
      <th></th>
      <th>Definition</th>
      <th>Formula</th>
      <th>API Metric ID</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <th>Ad Impressions</th>
      <td>The number of ads served to end-users.</td>
      <td>-</td>
      <td>ad_impressions</td>
    </tr>
    <tr>
      <th>Ad Revenue</th>
      <td>The revenue generated by serving in-app ads.</td>
      <td>-</td>
      <td>ad_revenue</td>
    </tr>
    <tr>
      <th>Ad Revenue (Cohort)</th>
      <td>The total ad revenue generated from users who installed or reinstalled your app within your selected timeframe up to the current date.&nbsp;It is cumulative and based on ad revenue reported by the Adjust SDK or recorded server-to-server.&nbsp;<br /><br />Example: If the selected time frame is Jan 1-31, and today is May 1st, the revenue is counted up to May 1st.</td>
      <td>-</td>
      <td>cohort_ad_revenue</td>
    </tr>
    <tr>
      <th>Ad RPM</th>
      <td>Ad Revenue per Mille Impressions. Your ad revenue per thousand ad impressions.</td>
      <td>(<code>Ad revenue</code> / <code>Ad impressions</code>) * 1000</td>
      <td>ad_rpm</td>
    </tr>
    <tr>
      <th>Revenue</th>
      <td>The revenue your app has generated within a selected timeframe based on revenue events reported by the Adjust SDK or as recorded server-to-server.</td>
      <td>-</td>
      <td>revenue</td>
    </tr>
    <tr>
      <th>Revenue (Cohort)</th>
      <td>The total in-app revenue generated from users who installed or reinstalled your app within your selected timeframe up to the current date. It is cumulative and based on events reported by the Adjust SDK or server-to-server.&nbsp;<br /><br />Example: If the selected time frame is Jan 1-31, and today is May 1st, the revenue is counted up to May 1st.</td>
      <td>-</td>
      <td>cohort_revenue</td>
    </tr>
    <tr>
      <th>All Revenue</th>
      <td>The amount of revenue generated by an app, calculated using all revenue sources.</td>
      <td><code>Ad Revenue</code> + <code>Revenue</code></td>
      <td>all_revenue</td>
    </tr>
    <tr>
      <th>All Revenue (Cohort)</th>
      <td>The total amount of in-app revenue and ad revenue generated from users who installed or reinstalled your app within your selected timeframe up to the current date. It is cumulative and based on events reported by the Adjust SDK or server-to-server.&nbsp;<br /><br />Example: If the selected time frame is Jan 1-31, and today is May 1st, the revenue is counted up to May 1st.</td>
      <td><code>Cohort Revenue</code> + <code>Cohort Ad Revenue</code></td>
      <td>cohort_all_revenue</td>
    </tr>
    <tr>
      <th>ARPDAU (All)</th>
      <td>Average revenue per daily active user, calculated using all sources of revenue.</td>
      <td><code>All Revenue Total </code>/ <code>Total DAU</code> in the date range</td>
      <td>arpdau</td>
    </tr>
    <tr>
      <th>ARPDAU (Ad)</th>
      <td>Average revenue per daily active user, calculated using only revenue from serving ads.</td>
      <td><code>Ad Revenue</code> / <code>Total DAU</code> in the date range</td>
      <td>arpdau_ad</td>
    </tr>
    <tr>
      <th>ARPDAU (IAP)</th>
      <td>Average revenue per daily active user, calculated using in-app purchase revenue.</td>
      <td><code>In-app Revenue</code> / <code>Total DAU</code> in the date range</td>
      <td>arpdau_iap</td>
    </tr>
    <tr>
      <th>Gross profit</th>
      <td>Revenue minus ad spend.</td>
      <td><code>All revenue</code> - <code>Ad Spend</code></td>
      <td>gross_profit</td>
    </tr>
    <tr>
      <th>Gross profit (Cohort)</th>
      <td>The cohorted gross profit.</td>
      <td><code>Cohort revenue</code> -&nbsp;<code>Ad Spend</code></td>
      <td>cohort_gross_profit</td>
    </tr>
    <tr>
      <th>Return On Investment (ROI)</th>
      <td>The cohorted gross profit divided by ad spend.</td>
      <td><code>Cohort gross profit</code> / <code>Ad</code><code>Spend</code></td>
      <td>return_on_investment</td>
    </tr>
    <tr>
      <th>Revenue Events</th>
      <td>The total number of revenue events that have been triggered.</td>
      <td>-</td>
      <td>revenue_events</td>
    </tr>
    <tr>
      <th>Revenue To Cost Ratio (RCR)</th>
      <td>The revenue-to-cost ratio.</td>
      <td><code>Cohort revenue</code> / <code></code><code>Ad Spend</code></td>
      <td>revenue_to_cost</td>
    </tr>
    <tr>
      <th>ROAS (All Revenue)</th>
      <td>Return on ad spend, calculated using all sources of revenue.</td>
      <td>(<code>Cohort revenue </code>+ <code>Cohort ad revenue</code>) / <code>Ad</code><code>Spend</code></td>
      <td>roas</td>
    </tr>
    <tr>
      <th>ROAS (Ad Revenue)</th>
      <td>Return on ad spend, calculated using only ad revenue.</td>
      <td><code>Cohort ad revenue</code>&nbsp;/&nbsp;<code>Ad Spend</code></td>
      <td>roas_ad</td>
    </tr>
    <tr>
      <th>ROAS (IAP Revenue)</th>
      <td>Return on ad spend, calculated using only in-app revenue.</td>
      <td><code>Cohort revenue</code>&nbsp;/&nbsp;<code>Ad Spend</code></td>
      <td>roas_iap</td>
    </tr>
  </tbody>
</table>

## [SKAdNetwork metrics](skad-metrics)

These metrics are related to Apple's SKAdNetwork. Read more about [Adjust's SKAdNetwork solutions](/en/article/ios-att-and-skadnetwork). 

<table>
  <thead>
    <tr>
      <th></th>
      <th>Definition</th>
      <th>Formula</th>
      <th>API Metric ID</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <th>Conversion Bit 1 - Conversion Bit 6 (SKAN)</th>
      <td>Returns the count of valid SKAN postbacks where the corresponding conversion event has been triggered.&nbsp;These metrics only have meaning when you are using the conversion events model.</td>
      <td>-</td>
      <td><code>conversion_1</code> to <code>conversion_6</code></td>
    </tr>
    <tr>
      <th>Conversion Value 0&nbsp;(SKAN)</th>
      <td>Returns the count of valid SKAN postbacks where the conversion value is equal to 0 (install).&nbsp; <br /><br />A conversion value of 0 is returned when the user has installed the app but not triggered any mapped conversion value criteria.</td>
      <td>-</td>
      <td>conversion_value_0</td>
    </tr>
    <tr>
      <th>Conversion Value 1 - Conversion Value 63&nbsp;(SKAN)</th>
      <td>Returns the count of valid SKAN postbacks with the corresponding conversion value. (1-63)&nbsp;</td>
      <td>-</td>
      <td><code>conversion_value_1</code>&nbsp;to <code>conversion_value_63</code></td>
    </tr>
    <tr>
      <th>Conversion Value greater than 0&nbsp;(SKAN)</th>
      <td>Returns the count of valid SKAN postbacks where the conversion value is greater than 0 (install).</td>
      <td><code>Valid Conversions (SKAN)</code> - <code>Conversion Value 0</code></td>
      <td>skad_conversion_value_gt_0</td>
    </tr>
    <tr>
      <th>Conversion Value Null</th>
      <td>The number of SKAN 3 postbacks and SKAN 4 1st postbacks received with a nulled conversion value. Null means an install took place, but further data has been hidden due to Apple's privacy framework.</td>
      <td>(<code>Installs (SKAN)</code>&nbsp;+ <code>Reinstalls (SKAN)</code>) -<code> Valid conversions</code></td>
      <td>skad_conversion_value_null</td>
    </tr>
    <tr>
      <th>Conversion Value Total (SKAN)</th>
      <td>Returns the sum of all conversion values as a single integer. The count of each conversion value is multiplied by its conversion value. <br /><br />For example: for conversion value 40 the total number of recorded occurrences is multiplied by 40 to give the resulting total conversion value.</td>
      <td>['conversion_value_0'... 'conversion_value_63'] * (0, 1, ... 63)</td>
      <td>conversion_value_total</td>
    </tr>
    <tr>
      <th>Conversion Value Null Rate (SKAN)</th>
      <td>The number of postbacks received with a conversion value of null divided by the number of SKAN conversions.</td>
      <td><code>Conversion Value Null (SKAN)</code> / <code>Total Conversions (SKAN)</code></td>
      <td>skad_conversion_value_null_rate</td>
    </tr>
    <tr>
      <th>Coarse conversion value null (1st Postback)</th>
      <td>The number of 1st postbacks received with a nulled coarse conversion value. Null means an install took place, but further data has been hidden due to Apple's privacy framework.</td>
      <td></td>
      <td>skad_coarse_conversion_values_null_0</td>
    </tr>
    <tr>
      <th>Coarse conversion value none (1st Postback)</th>
      <td>The number of 1st postbacks received with a coarse conversion value of none. None is sent by Apple whenever none of the conditions that are set for low, medium, and high were met.</td>
      <td></td>
      <td>skad_coarse_conversion_values_none_0</td>
    </tr>
    <tr>
      <th>Coarse conversion value low (1st Postback)</th>
      <td>The number of 1st postbacks received with a coarse conversion value of low. P1 low always = Install.</td>
      <td></td>
      <td>skad_coarse_conversion_values_low_0</td>
    </tr>
    <tr>
      <th>Coarse conversion value medium (1st Postback)</th>
      <td>The number of 1st postbacks received with a coarse conversion value of medium.</td>
      <td></td>
      <td>skad_coarse_conversion_values_medium_0</td>
    </tr>
    <tr>
      <th>Coarse conversion value high (1st Postback)</th>
      <td>The number of 1st postbacks received with a coarse conversion value of high.</td>
      <td></td>
      <td>skad_coarse_conversion_values_high_0</td>
    </tr>
    <tr>
      <th>Coarse conversion value null (2nd Postback)</th>
      <td>The number of 2nd postbacks received with a nulled coarse conversion value. Null means a session took place, but further data has been hidden due to Apple's privacy framework.</td>
      <td></td>
      <td>skad_coarse_conversion_values_null_1</td>
    </tr>
    <tr>
      <th>Coarse conversion value none (2nd Postback)</th>
      <td>The number of 2nd postbacks received with a coarse conversion value of none. None is sent by Apple whenever none of the conditions that are set for low, medium, and high were met.</td>
      <td></td>
      <td>skad_coarse_conversion_values_none_1</td>
    </tr>
    <tr>
      <th>Coarse conversion value low (2nd Postback)</th>
      <td>The number of 2nd postbacks received with a coarse conversion value of low.</td>
      <td></td>
      <td>skad_coarse_conversion_values_low_1</td>
    </tr>
    <tr>
      <th>Coarse conversion value medium (2nd Postback)</th>
      <td>The number of 2nd postbacks received with a coarse conversion value of medium.</td>
      <td></td>
      <td>skad_coarse_conversion_values_medium_1</td>
    </tr>
    <tr>
      <th>Coarse conversion value high (2nd Postback)</th>
      <td>The number of 2nd postbacks received with a coarse conversion value of high.</td>
      <td></td>
      <td>skad_coarse_conversion_values_high_1</td>
    </tr>
    <tr>
      <th>Coarse conversion value null (3rd Postback)</th>
      <td>The number of 3rd postbacks received with a nulled coarse conversion value. Null means a session took place, but further data has been hidden due to Apple's privacy framework.</td>
      <td></td>
      <td>skad_coarse_conversion_values_null_2</td>
    </tr>
    <tr>
      <th>Coarse conversion value none (3rd Postback)</th>
      <td>The number of 3rd postbacks received with a coarse conversion value of none. None is sent by Apple whenever none of the conditions that are set for low, medium, and high were met.</td>
      <td></td>
      <td>skad_coarse_conversion_values_none_2</td>
    </tr>
    <tr>
      <th>Coarse conversion value low (3rd Postback)</th>
      <td>The number of 3rd postbacks received with a coarse conversion value of low.</td>
      <td></td>
      <td>skad_coarse_conversion_values_low_2</td>
    </tr>
    <tr>
      <th>Coarse conversion value medium (3rd Postback)</th>
      <td>The number of 3rd postbacks received with a coarse conversion value of medium.</td>
      <td></td>
      <td>skad_coarse_conversion_values_medium_2</td>
    </tr>
    <tr>
      <th>Coarse conversion value high (3rd Postback)</th>
      <td>The number of 3rd postbacks received with a coarse conversion value of high.</td>
      <td></td>
      <td>skad_coarse_conversion_values_high_2</td>
    </tr>
    <tr>
      <th>Event eCR (SKAN) - Min</th>
      <td>The effective minimum conversion rate per action from SKAN for a given event.</td>
      <td><code>{event slug} Events Min</code> / <code>Valid conversions</code></td>
      <td>{event_slug}_skan_event_ecr_min</td>
    </tr>
    <tr>
      <th>Event eCR (SKAN) - Avg</th>
      <td>The effective average conversion rate per action from SKAN for a given event.</td>
      <td><code>{event slug} Events Est</code> / <code>Valid conversions</code></td>
      <td>{event_slug}_skan_event_ecr_est</td>
    </tr>
    <tr>
      <th>Event eCR (SKAN) - Max</th>
      <td>The effective maximum conversion rate per action from SKAN for a given event.</td>
      <td><code>{event slug} Events Max</code> / <code>Valid conversions</code></td>
      <td>{event_slug}_skan_event_ecr_max</td>
    </tr>
    <tr>
      <th>Installs (SKAN)</th>
      <td>Returns the count of valid SKAN postbacks where redownload = false. SKAN postbacks are valid when the attribution signature is correct.</td>
      <td>Count of valid SKAN postbacks with redownload = <code>false</code></td>
      <td>skad_installs</td>
    </tr>
    <tr>
      <th>Qualifiers (SKAN)&nbsp;</th>
      <td>Returns the number of installs that had a touchpoint with the network, but did not win the final SKAN attribution. This means the SKAN postback returned a&nbsp;<code>did-win: false</code>&nbsp;flag.</td>
      <td>-</td>
      <td>skad_qualifiers</td>
    </tr>
    <tr>
      <th>Invalid Payloads (SKAN)</th>
      <td><a href="https://developer.apple.com/documentation/storekit/skadnetwork/verifying_an_install_validation_postback" target="_blank"></a>Count of SKAN postbacks (install/reinstall) that were invalid after <a href="https://developer.apple.com/documentation/storekit/skadnetwork/verifying_an_install-validation_postback">verification of the attribution signature</a>.&nbsp;</td>
      <td>Calculated as the count of SKAN postbacks that didn’t have the correct attribution-signature.</td>
      <td>invalid_payloads</td>
    </tr>
    <tr>
      <th>Reinstalls (SKAN)</th>
      <td>Returns the count of valid SKAN postbacks where redownload = true. SKAN postbacks are valid when the attribution signature is correct.</td>
      <td>Count of valid SKAN postbacks with redownload = <code>true</code></td>
      <td>skad_reinstalls</td>
    </tr>
    <tr>
      <th>Total conversions (SKAN)</th>
      <td>The total number of conversions (installs and reinstalls) reported by SKAN.</td>
      <td><code>Installs (SKAN)</code> + <code>Reinstalls (SKAN)</code></td>
      <td>skad_total_installs</td>
    </tr>
    <tr>
      <th>Valid Conversions (SKAN)</th>
      <td>Counter of SKAN postbacks (install/reinstall) that have a valid conversion value attached. It shows the number of postbacks where the conversion value was not null. Valid conversion values include coarse and fine (0-63) conversion values.</td>
      <td><code>Installs (SKAN)</code> + <code>Reinstalls (SKAN)</code> - <code>Conversion Value Null</code></td>
      <td>valid_conversions</td>
    </tr>
    <tr>
      <th>eCPA (SKAN)</th>
      <td>The Effective Cost per Action of a specific event.&nbsp;</td>
      <td>(<code>Ad Spend (SKAN)</code> * <code>Valid Conversions (SKAN)</code>) / (<code>Unpacked Event (SKAN)</code> * <code>Installs (SKAN)</code>)</td>
      <td>{event_slug}_skan_ecpa</td>
    </tr>
    <tr>
      <th>Ad Spend (SKAN)</th>
      <td>The amount spent on ads for SKAN campaigns as reported by the network API.</td>
      <td>-</td>
      <td>network_ad_spend_skan</td>
    </tr>
    <tr>
      <th>ROAS (SKAN) - Min</th>
      <td>Minimum return on ad spend, calculated using only revenue and spend data from SKAN.</td>
      <td><code>(Total Revenue (SKAN Min) * Installs (SKAN)) / (Ad Spend (SKAN) * Valid Conversions)</code></td>
      <td>skad_revenue_min_roas</td>
    </tr>
    <tr>
      <th>ROAS (SKAN) - Avg</th>
      <td>Average return on ad spend, calculated using only revenue and spend data from SKAN.</td>
      <td><code>(Total Revenue (SKAN Avg) * Installs (SKAN)) / (Ad Spend (SKAN) * Valid Conversions)</code></td>
      <td>skad_revenue_est_roas</td>
    </tr>
    <tr>
      <th>ROAS (SKAN) - Max</th>
      <td>Maximum return on ad spend, calculated using only revenue and spend data from SKAN.</td>
      <td><code>(Total Revenue (SKAN Max) * Installs (SKAN)) / (Ad Spend (SKAN) * Valid Conversions)</code></td>
      <td>skad_revenue_max_roas</td>
    </tr>
    <tr>
      <th>ROI (SKAN) - Min</th>
      <td>The minimum return on investment for SKAN campaigns, calculated using only SKAN postback data and network ad spend data.</td>
      <td><code>(Revenue (SKAN Min) * Installs (SKAN)) / (Ad Spend (SKAN) * Valid Conversions) - 1</code></td>
      <td>skad_revenue_min_roi</td>
    </tr>
    <tr>
      <th>ROI (SKAN) - Avg</th>
      <td>The average return on investment for SKAN campaigns, calculated using only SKAN postback data and network ad spend data.</td>
      <td><code>(Revenue (SKAN Avg) * Installs (SKAN)) / (Ad Spend (SKAN)) * Valid Conversions) - 1</code></td>
      <td>skad_revenue_est_roi</td>
    </tr>
    <tr>
      <th>ROI (SKAN) - Max</th>
      <td>The maximum return on investment for SKAN campaigns, calculated using only SKAN postback data and network ad spend data.</td>
      <td><code>(Revenue (SKAN Max) * Installs (SKAN)) / (Ad Spend (SKAN) * Valid Conversions) - 1</code></td>
      <td>skad_revenue_max_roi</td>
    </tr>
    <tr>
      <th>RPU - Ad Rev (SKAN) - Min</th>
      <td>The minimum ad revenue per user from SKAN.</td>
      <td><code>SKAN Ad Revenue - Min</code> / <code>Valid conversions</code></td>
      <td>skan_ad_rpu_min</td>
    </tr>
    <tr>
      <th>RPU - Ad Rev (SKAN) - Avg</th>
      <td>The average ad revenue per user from SKAN.</td>
      <td><code>SKAN Ad Revenue - Est</code> / <code>Valid conversions</code></td>
      <td>skan_ad_rpu_est</td>
    </tr>
    <tr>
      <th>RPU - Ad Rev (SKAN) - Max</th>
      <td>The maximum ad revenue per user from SKAN.</td>
      <td><code>SKAN Ad Revenue - Max</code> / <code>Valid conversions</code></td>
      <td>skan_ad_rpu_max</td>
    </tr>
    <tr>
      <th>RPU - IAP (SKAN IAP) - Min</th>
      <td>The minimum in-app purchase revenue per user from SKAN.</td>
      <td></td>
      <td>skan_iap_rpu_min</td>
    </tr>
    <tr>
      <th>RPU - IAP (SKAN) - Avg</th>
      <td>The average in-app revenue purchase per user from SKAN.</td>
      <td></td>
      <td>skan_iap_rpu_est</td>
    </tr>
    <tr>
      <th>RPU - IAP (SKAN) - Max</th>
      <td>The maximum in-app purchase revenue per user from SKAN.</td>
      <td></td>
      <td>skan_iap_rpu_max</td>
    </tr>
    <tr>
      <th>Event RPU (SKAN) - Min</th>
      <td>The minimum revenue per user from SKAN &nbsp;for a given event.</td>
      <td><code>{event slug} Revenue - Min</code> / <code>Valid conversions</code></td>
      <td>{event_slug}_skan_event_rpu_min</td>
    </tr>
    <tr>
      <th>Event RPU (SKAN) - Avg</th>
      <td>The average revenue per user from SKAN &nbsp;for a given event.</td>
      <td><code>{event slug} Revenue - Est</code> / <code>Valid conversions</code></td>
      <td>{event_slug}_skan_event_rpu_est</td>
    </tr>
    <tr>
      <th>Event RPU (SKAN) - Max</th>
      <td>The maximum revenue per user from SKAN &nbsp;for a given event.</td>
      <td><code>{event slug} Revenue - Max</code> / <code>Valid conversions</code></td>
      <td>{event_slug}_skan_event_rpu_max</td>
    </tr>
    <tr>
      <th>Total RPU (SKAN) - Min</th>
      <td>The minimum total revenue per user from SKAN.</td>
      <td><code>SKAN Total Revenue - Min</code> / <code>Valid conversions</code></td>
      <td>skan_total_rpu_min</td>
    </tr>
    <tr>
      <th>Total RPU (SKAN) - Avg</th>
      <td>The average total revenue per user from SKAN.</td>
      <td><code>SKAN Total Revenue - Est</code> / <code>Valid conversions</code></td>
      <td>skan_total_rpu_est</td>
    </tr>
    <tr>
      <th>Total RPU (SKAN) - Max</th>
      <td>The maximum total revenue per user from SKAN.</td>
      <td><code>SKAN Total Revenue - Max</code> / <code>Valid conversions</code></td>
      <td>skan_total_rpu_max</td>
    </tr>
    <tr>
      <th>eCPI (SKAN)</th>
      <td>Effective cost per SKAN install, calculated by dividing ad spend by installs.</td>
      <td><code>Ad Spend (SKAN) / Installs (SKAN)</code></td>
      <td>skad_ecpi</td>
    </tr>
    <tr>
      <th>Ad Revenue (SKAN) - Min</th>
      <td>The minimum ad revenue generated according to the range of the triggered conversion value.</td>
      <td>-</td>
      <td>skad_ad_revenue_min</td>
    </tr>
    <tr>
      <th>Ad Revenue (SKAN) - Avg</th>
      <td>The average ad revenue generated according to the range of the triggered conversion value.</td>
      <td>-</td>
      <td>skad_ad_revenue_est</td>
    </tr>
    <tr>
      <th>Ad Revenue (SKAN) - Max</th>
      <td>The maximum ad revenue generated according to the range of the triggered conversion value.</td>
      <td>-</td>
      <td>skad_ad_revenue_max</td>
    </tr>
    <tr>
      <th>In-App Revenue (SKAN) - Min</th>
      <td>The minimum in-app revenue earned from your conversion value ranges as reported from SKAN postbacks.</td>
      <td>-</td>
      <td>iap_revenue_revenue_min</td>
    </tr>
    <tr>
      <th>In-App Revenue (SKAN) - Avg</th>
      <td>The average in-app revenue earned from your conversion value ranges as reported from SKAN postbacks.</td>
      <td>-</td>
      <td>iap_revenue_revenue_est</td>
    </tr>
    <tr>
      <th>In-App Revenue (SKAN) - Max</th>
      <td>The maximum in-app revenue earned from your conversion value ranges as reported from SKAN postbacks.</td>
      <td>-</td>
      <td>iap_revenue_revenue_max</td>
    </tr>
    <tr>
      <th>Total Revenue (SKAN) - Min</th>
      <td>The minimum total revenue unpacked from Conversion Value revenue buckets, including all revenue sources.</td>
      <td>-</td>
      <td>skan_total_revenue_min</td>
    </tr>
    <tr>
      <th>Total Revenue (SKAN) - Avg</th>
      <td>The average total revenue unpacked from Conversion Value revenue buckets, including all revenue sources.</td>
      <td>-</td>
      <td>skan_total_revenue_est</td>
    </tr>
    <tr>
      <th>Total Revenue (SKAN) - Max</th>
      <td>The maximum total revenue unpacked from Conversion Value revenue buckets, including all revenue sources.</td>
      <td>-</td>
      <td>skan_total_revenue_max</td>
    </tr>
    <tr>
      <th>Event (SKAN) - Min</th>
      <td>The number of events calculated from the SKAN postback, using the events count condition for a specific named event.</td>
      <td>-</td>
      <td>{event_slug}_events_min</td>
    </tr>
    <tr>
      <th>Event (SKAN) - Max</th>
      <td>The number of events calculated from the SKAN postback, using the events count condition for a specific named event.</td>
      <td>-</td>
      <td>{event_slug}_events_max</td>
    </tr>
    <tr>
      <th>Event (SKAN) - Avg</th>
      <td>The number of events calculated from the SKAN postback, using the events count condition for a specific named event.</td>
      <td>-</td>
      <td>{event_slug}_events_est</td>
    </tr>
    <tr>
      <th>Event Revenue (SKAN) - Min</th>
      <td>The revenue from SKAN postbacks based on the revenue earned from a particular event.</td>
      <td>Example: If Conversion Value 42 corresponds to a Purchase event, triggered 10–20 times, then the metric is going to be equal to 10.</td>
      <td>{event_slug}_revenue_min</td>
    </tr>
    <tr>
      <th>Event Revenue (SKAN) - Max</th>
      <td>The revenue from SKAN postbacks based on the revenue earned from a particular event.</td>
      <td>Example: If Conversion Value 42 corresponds to a Purchase event, triggered 10–20 times, then the metric is going to be equal to 20.</td>
      <td>{event_slug}_revenue_max</td>
    </tr>
    <tr>
      <th>Event Revenue (SKAN) - Avg</th>
      <td>The revenue from SKAN postbacks based on the revenue earned from a particular event.</td>
      <td>Example: If Conversion Value 42 corresponds to a Purchase event, triggered 10–20 times, then the metric is going to be equal to 15.</td>
      <td>{event_slug}_revenue_est</td>
    </tr>
    <tr>
      <th>Total Revenue Events (SKAN) - Min</th>
      <td>Total revenue events include the following revenue event types: In-App purchases and Ad Revenue.<br /><br />This metric returns the lowest number of total revenue events triggered within your conversion value ranges.</td>
      <td>-</td>
      <td>general revenue_events_min</td>
    </tr>
    <tr>
      <th>Total Revenue Events (SKAN) - Avg</th>
      <td>Total revenue events include the following&nbsp;revenue event types: In-App purchases and Ad Revenue .<br /><br />This metric returns the average number of total revenue events triggered within your conversion value ranges.</td>
      <td>-</td>
      <td>general revenue_events_est</td>
    </tr>
    <tr>
      <th>Total Revenue Events (SKAN) - Max</th>
      <td>Total revenue events include the following&nbsp;revenue event types: In-App purchases and Ad Revenue.<br /><br />This metric returns the highest number of total revenue events triggered within your conversion value ranges.</td>
      <td>-</td>
      <td>general revenue_events_max</td>
    </tr>
  </tbody>
</table>

<callout type="note">
The following SKAN metrics are only available on request. Reach out to your Technical Account Manager or support@adjust.com for access.
</callout>

<table>
  <thead>
    <tr>
      <th></th>
      <th>Description</th>
      <th>Formula</th>
      <th>API Metric ID</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <th>Direct Total Installs (SKAN)</th>
      <td>Number of installs and reinstalls reported directly by SKAdNetwork.</td>
      <td><code>Direct Installs + Direct Reinstalls</code></td>
      <td>skad_direct_total_installs</td>
    </tr>
    <tr>
      <th>Direct Installs (SKAN)</th>
      <td>Returns the count of valid postbacks sent directly from SKAdNetwork where redownload = false.</td>
      <td></td>
      <td>skad_direct_installs</td>
    </tr>
    <tr>
      <th>Direct Reinstalls (SKAN)</th>
      <td>Returns the count of valid postbacks sent directly from SKAdNetwork where redownload = true.</td>
      <td></td>
      <td>skad_direct_reinstalls</td>
    </tr>
    <tr>
      <th>Direct Invalid Payloads (SKAN)</th>
      <td>Count of postbacks (install/reinstall) sent directly from SKAdNetwork that were invalid after verification of the attribution signature.&nbsp;</td>
      <td></td>
      <td>skad_direct_invalid_payloads</td>
    </tr>
    <tr>
      <th>Direct Valid Conversions (SKAN)</th>
      <td>Count of postbacks (install/reinstall) sent directly from SKAdNetwork that have a valid conversion value attached. This shows the number of postbacks where the conversion value was not null. Valid conversion values include coarse and fine (0-63) conversion values.</td>
      <td></td>
      <td>skad_direct_valid_conversions</td>
    </tr>
    <tr>
      <th>Direct Conversion Value Null (SKAN)</th>
      <td>Returns the count of all valid postbacks sent directly from SKAdNetwork where the conversion value is null, or the count of all installs and reinstalls minus those with a conversion value not greater than or equal to 0.</td>
      <td><code>(Direct Installs + Direct Reinstalls) - Direct Valid Conversions</code></td>
      <td>skad_direct_conversion_value_null</td>
    </tr>
    <tr>
      <th>Direct Conversion Value Greater Than 0 (SKAN)</th>
      <td>Returns the count of valid postbacks sent directly from SKAdNetwork where the conversion value is greater than 0 (install).</td>
      <td><code>Direct Valid Conversions - Direct Conversion Value 0</code></td>
      <td>skad_direct_conversion_value_gt_0</td>
    </tr>
    <tr>
      <th>Direct Conversion Bit 1 - Direct Conversion Bit 6 (SKAN)</th>
      <td>Returns the count of valid postbacks sent directly from SKAdNetwork where the corresponding conversion event has been triggered.</td>
      <td></td>
      <td>skad_direct_conversion_1 to&nbsp;skad_direct_conversion_6</td>
    </tr>
    <tr>
      <th>Direct Conversion Value 0 - Direct Conversion Value 63 (SKAN)</th>
      <td>Returns the count of valid postbacks sent directly from SKAdNetwork with the corresponding conversion value. (1-63)&nbsp;</td>
      <td></td>
      <td>skad_direct_conversion_value_0 to&nbsp;skad_direct_conversion_value_63</td>
    </tr>
  </tbody>
</table>

## [Subscription metrics](subscription-metrics)

<table>
  <colgroup>
    <col />
    <col />
    <col />
    <col width="250" />
  </colgroup>
  <thead>
    <tr>
      <th>Metric</th>
      <th>Definition</th>
      <th>Formula</th>
      <th>API Metric ID</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <th colspan="4"><b>Events</b></th>
    </tr>
    <tr>
      <th>Activations</th>
      <td>Number of times users activated subscriptions.</td>
      <td>-</td>
      <td><code>subscrevnt_activation_events</code></td>
    </tr>
    <tr>
      <th>(iOS only) Billing retry</th>
      <td>Number of times where a trial has expired and is not canceled (the user did not unsubscribe from the product), and no billing issues occurred.</td>
      <td>-</td>
      <td><code>subscrevnt_billing_retry_events</code></td>
    </tr>
    <tr>
      <th>Cancellations</th>
      <td>Number of times users cancelled subscriptions.</td>
      <td>-</td>
      <td><code>subscrevnt_cancellation_events</code></td>
    </tr>
    <tr>
      <th>Discounted offers</th>
      <td>Number of times users activated subscriptions using discounted offers.</td>
      <td>-</td>
      <td><code>subscrevnt_discounted_offer_events</code></td>
    </tr>
    <tr>
      <th>Expirations</th>
      <td>Number of times where a subscription has expired.</td>
      <td>-</td>
      <td><code>subscrevnt_expiration_events</code></td>
    </tr>
    <tr>
      <th>First conversion<br />
        <ul>
          <li>For previous version of subscriptions</li>
        </ul>
      </th>
      <td>Number of times users triggered the first conversion event.</td>
      <td>-</td>
      <td><code>subscrevnt_first_conversion_events</code></td>
    </tr>
    <tr>
      <th>Grace period</th>
      <td>Number of times where a subscription has entered grace period.</td>
      <td>-</td>
      <td><code>subscrevnt_grace_period_events</code></td>
    </tr>
    <tr>
      <th>(Android only) On hold</th>
      <td>Number of times where a subscription has entered account hold.</td>
      <td>-</td>
      <td><code>subscrevnt_on_hold_events</code></td>
    </tr>
    <tr>
      <th>(Android only) Paused</th>
      <td>Number of times where a subscription has been paused.</td>
      <td>-</td>
      <td><code>subscrevnt_paused_events</code></td>
    </tr>
    <tr>
      <th>Price accepted</th>
      <td>Number of times where a user successfully confirmed subscription price change.</td>
      <td>-</td>
      <td><code>subscrevnt_price_accepted_events</code></td>
    </tr>
    <tr>
      <th>(iOS only) Price declined&nbsp;</th>
      <td>Number of times where a user declined subscription price change.</td>
      <td>-</td>
      <td><code>subscrevnt_price_declined_events</code></td>
    </tr>
    <tr>
      <th>Reactivations</th>
      <td>Number of times users reactivated subscriptions.</td>
      <td>-</td>
      <td><code>subscrevnt_reactivation_events</code></td>
    </tr>
    <tr>
      <th>Renewals</th>
      <td>Number of times users renewed subscriptions.</td>
      <td>-</td>
      <td><code>subscrevnt_renewal_events</code></td>
    </tr>
    <tr>
      <th>Renewals from billing retry</th>
      <td>Number of times where a user successfully renewed the transaction after a billing issue was resolved.</td>
      <td>-</td>
      <td><code>subscrevnt_renewal_from_billing_retry_events</code></td>
    </tr>
    <tr>
      <th>(iOS only) Refunds</th>
      <td>Number of times where the transaction for a subscription has been refunded.</td>
      <td>-</td>
      <td><code>subscrevnt_refund_events</code></td>
    </tr>
    <tr>
      <th>Revoked</th>
      <td>Number of times where a user revoked a subscription before expiration date.</td>
      <td>-</td>
      <td><code>subscrevnt_revoked_events</code></td>
    </tr>
    <tr>
      <th>Trials started</th>
      <td>Number of times users started trials.</td>
      <td>-</td>
      <td><code>subscrevnt_trial_started_events</code></td>
    </tr>
    <tr>
      <th colspan="4">Revenue</th>
    </tr>
    <tr>
      <th>Activation revenue</th>
      <td>Revenue from events where users activated a subscription product for the first time.</td>
      <td>-</td>
      <td><code>subscrevnt_activation_revenue</code></td>
    </tr>
    <tr>
      <th>Discounted offer revenue</th>
      <td>Revenue from events where a new subscription was purchased at a reduced price.</td>
      <td>-</td>
      <td><code>subscrevnt_discounted_offer_revenue</code></td>
    </tr>
    <tr>
      <th>Reactivation revenue</th>
      <td>Revenue from events where users who subscribed through a trial, offer, or activation, then cancelled it, and then reactivated it.</td>
      <td>-</td>
      <td><code>subscrevnt_reactivation_revenue</code></td>
    </tr>
    <tr>
      <th>(iOS only) Refund revenue</th>
      <td>Revenue from refunded transactions.</td>
      <td>-</td>
      <td><code>subscrevnt_refund_revenue</code></td>
    </tr>
    <tr>
      <th>Renewal revenue</th>
      <td>Revenue from events where users successfully renewed the subscription.</td>
      <td>-</td>
      <td><code>subscrevnt_renewal_revenue</code></td>
    </tr>
    <tr>
      <th>Renewal from billing retry revenue</th>
      <td>Revenue from events where users successfully renewed the transaction after a billing issue was resolved.</td>
      <td>-</td>
      <td><code>subscrevnt_renewal_from_billing_retry_revenue</code></td>
    </tr>
    <tr>
      <th>Subscription revenue</th>
      <td>Total revenue from all events.</td>
      <td><code>Activation revenue + Discounted offer revenue + Reactivation revenue + Refund revenue + Renewal revenue + Renewal from billing retry revenue + Unknown revenue</code></td>
      <td><code>subscrevnt_revenue</code></td>
    </tr>
    <tr>
      <th>Unknown revenue</th>
      <td>Revenue from undefined events.&nbsp;</td>
      <td>-</td>
      <td><code>subscrevnt_unknown_revenue</code></td>
    </tr>
  </tbody>
</table>

### [Cumulative subscription cohort metrics](cumulative-subscription-cohort-metrics)

<table>
  <colgroup>
    <col />
    <col />
    <col />
    <col width="250" />
  </colgroup>
  <thead>
    <tr>
      <th>Metric</th>
      <th>Definition</th>
      <th>Formula</th>
      <th>API Metric ID</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <th colspan="4">Events</th>
    </tr>
    <tr>
      <th>N days&nbsp;Activations Total</th>
      <td>Cumulative number of times users activated subscriptions during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_activation_events_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>(iOS only)&nbsp;N days Billing retry Total</th>
      <td>Cumulative number of times where a trial has expired and is not canceled (the user did not unsubscribe from the product), and no billing issues occurred during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_billing_retry_events_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Cancellations Total</th>
      <td>Cumulative number of times users cancelled subscriptions during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_cancellation_events_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Discounted offers Total</th>
      <td>Cumulative number of times users activated subscriptions using discounted offers during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_discounted_offer_events_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Expirations Total</th>
      <td>Cumulative number of times subscriptions ended during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_expiration_events_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;First conversion Total<br />
        <ul>
          <li>For previous version of subscriptions</li>
        </ul>
      </th>
      <td>Cumulative number of times users triggered the first conversion event during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_first_conversion_events_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Grace period Total</th>
      <td>Cumulative number of times where a subscription has entered the grace period during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_grace_period_events_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>(Android only) N days&nbsp;On hold Total</th>
      <td>Cumulative number of times where a subscription has entered account hold during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_on_hold_events_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>(Android only) N days&nbsp;Paused Total</th>
      <td>Cumulative number of times where a subscription has been paused during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_paused_events_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Price accepted Total</th>
      <td>Cumulative number of times where a user successfully confirmed subscription price change during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_price_accepted_events_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>(iOS only)&nbsp;N days&nbsp;Price declined Total</th>
      <td>Cumulative number of times where a user declined subscription price change during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_price_declined_events_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Reactivations Total</th>
      <td>Cumulative number of times users reactivated subscriptions during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_reactivation_events_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>(iOS only)&nbsp;N days&nbsp;Refunds Total</th>
      <td>Cumulative number of times subscriptions ended immediately and the users were refunded during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_refund_events_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Renewals Total</th>
      <td>Cumulative number of times users renewed subscriptions during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_renewal_events_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Renewal from billing retry Total</th>
      <td>Cumulative number of times where a user successfully renewed the transaction after a billing issue was resolved during the selected cohort period.&nbsp;</td>
      <td>-</td>
      <td><code>subscription_renewal_from_billing_retry_events_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Revoked Total</th>
      <td>Cumulative number of times where a user revoked a subscription before expiration date during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_revoked_events_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Trials started Total</th>
      <td>Cumulative number of times users started trials during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_trial_started_events_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th colspan="4">Revenue</th>
    </tr>
    <tr>
      <th>N days Revenue total (activations)</th>
      <td>Cumulative revenue from events where users activated a subscription product for the first time during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_activation_revenue_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Revenue total (discounted offers)</th>
      <td>Cumulative revenue from events where a new subscription was purchased at a reduced price during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_discounted_offer_revenue_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Revenue total (reactivations)</th>
      <td>Cumulative revenue from events where users who subscribed through a trial, offer, or activation, then cancelled it, and then reactivated it during the selected cohort period</td>
      <td>-</td>
      <td><code>subscription_reactivation_revenue_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>(iOS only)&nbsp;N days&nbsp;Revenue total (refunds)</th>
      <td>Cumulative revenue from refunded transactions during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_refund_revenue_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Revenue total (renewals)</th>
      <td>Cumulative revenue from events where users successfully renewed the subscription during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_renewal_revenue_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Revenue total (retries)</th>
      <td>Cumulative revenue from events where users successfully renewed the transaction after a billing issue was resolved during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_renewal_from_billing_retry_revenue_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days Revenue total (other)</th>
      <td>Cumulative revenue from undefined events during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_unknown_revenue_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days Subscription Revenue Total</th>
      <td>Cumulative subscription revenue generated during the selected cohort period.</td>
      <td><code>{cohort_period} Revenue total (activations) + {cohort_period} Revenue total (discounted offers) + {cohort_period} Revenue total (reactivations) + {cohort_period} Revenue total (refunds) + {cohort_period} Revenue total (renewals) + {cohort_period} Revenue total (retries) + {cohort_period} Revenue total (other)</code></td>
      <td><code>subscription_revenue_total_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days Subscription Revenue Total in Cohort</th>
      <td>Total subscription-related revenue attributed to users within the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_subscription_revenue_total_in_cohort_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days Conversion rates</th>
      <td>Number of times a subscription is converted N days after the previous event state.<br /><br /><b>Event from:</b><br /> <br />
        <ul>
          <li>Install</li>
          <li>Trials started</li>
          <li>Discounted offer</li>
          <li>Activation</li>
          <li>Renewal</li>
        </ul><br /><b>Event to:</b><br />Any of the subscription events<br /> <br /> <b>Examples:</b><br /> <br />
        <ul>
          <li>install → trial</li>
          <li>install → activation</li>
          <li>trial → renewal</li>
          <li>activation → renewal</li>
          <li>discounted offer → renewal</li>
          <li>renewal → cancellation</li>
        </ul>
      </td>
      <td><code>{cohort_period} {event_to} total / {cohort_period} {event_from} total</code></td>
      <td><code>subscription_{event_from}_to_{event_to}_rate_{cohort_period}</code></td>
    </tr>
  </tbody>
</table>

### [Non-cumulative subscription cohort metrics](non-cumulative-subscription-cohort-metrics)

<table>
  <colgroup>
    <col />
    <col />
    <col />
    <col width="250" />
  </colgroup>
  <thead>
    <tr>
      <th>Metric</th>
      <th>Definition</th>
      <th>Formula</th>
      <th>API Metric ID</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <th colspan="4">Events</th>
    </tr>
    <tr>
      <th>N days&nbsp;Activations</th>
      <td>Number of times a user activated the paid subscription N days after the install or reattribution during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_activation_events_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>(iOS only)&nbsp;N days Billing retry</th>
      <td>Number of times where a trial has expired and is not canceled (the user did not unsubscribe from the product), and no billing issues occurred during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_billing_retry_events_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Cancellations</th>
      <td>Number of times a user canceled their paid subscription N days after installation or reattribution during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_cancellation_events_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Discounted offers</th>
      <td>Number of times users activated subscriptions using discounted offers during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_discounted_offer_events_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Expirations</th>
      <td>Number of times a subscription is expired N days after installation or reattribution during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_expiration_events_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;First conversion<br />
        <ul>
          <li>For previous version of subscriptions</li>
        </ul>
      </th>
      <td>Number of times users triggered the first conversion event during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_first_conversion_events_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Grace period</th>
      <td>Number of times where a subscription has entered grace period during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_grace_period_events_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>(Android only) N days&nbsp;On hold</th>
      <td>Number of times where a subscription has entered account hold during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_on_hold_events_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>(Android only) N days&nbsp;Paused</th>
      <td>Number of times where a subscription has been paused during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_paused_events_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Price accepted</th>
      <td>Number of times where a user successfully confirmed a subscription price change during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_price_accepted_events_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>(iOS only)&nbsp;N days&nbsp;Price declined</th>
      <td>Number of times where a user declined a subscription price change during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_price_declined_events_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Reactivations</th>
      <td>Number of times a user reactivated their paid subscription N days after installation or reattribution during the selected cohort period.<br />Reactivation occurs if a user previously had a paid subscription that expired.</td>
      <td>-</td>
      <td><code>subscription_reactivation_events_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>(iOS only)&nbsp;N days&nbsp;Refunds</th>
      <td>Number of times a user refunded their paid subscription N days after installation or reattribution during the selected cohort period. A refund is different from a cancellation because it takes effect immediately.</td>
      <td>-</td>
      <td><code>subscription_refund_events_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Renewals</th>
      <td>Number of times a user renewed their paid subscription N days after installation or reattribution during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_renewal_events_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Renewals from billing retry</th>
      <td>Number of times where a user successfully renewed the transaction after a billing issue was resolved during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_renewal_from_billing_retry_events_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Revoked</th>
      <td>Number of times where a user revoked a subscription before expiration date during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_revoked_events_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Trials started</th>
      <td>Number of times a user started the free trial period N days after the install or reattribution during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_trial_started_events_{cohort_period}</code></td>
    </tr>
    <tr>
      <th colspan="4"><b>Revenue</b></th>
    </tr>
    <tr>
      <th>N days&nbsp;Activation revenue</th>
      <td>Revenue from events where users activated a subscription product for the first time during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_activation_revenue_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Discounted offer revenue</th>
      <td>Revenue from events where a new subscription was purchased at a reduced price during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_discounted_offer_revenue_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Reactivation revenue</th>
      <td>Revenue from events where users who subscribed through a trial, offer, or activation, then cancelled it, and then reactivated it during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_reactivation_revenue_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>(iOS only)&nbsp;N days&nbsp;Refund revenue</th>
      <td>Revenue from refunded transactions during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_refund_revenue_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Renewal revenue</th>
      <td>Revenue from events where users successfully renewed the subscription during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_renewal_revenue_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days&nbsp;Renewal from billing retry revenue</th>
      <td>Revenue from events where users successfully renewed the transaction after a billing issue was resolved during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_renewal_from_billing_retry_revenue_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days Unknown revenue</th>
      <td>Revenue from undefined events during the selected cohort period.</td>
      <td>-</td>
      <td><code>subscription_unknown_revenue_{cohort_period}</code></td>
    </tr>
    <tr>
      <th>N days Subscription Revenue</th>
      <td>Revenue from all subscription events for the selected cohort period.</td>
      <td><code>{cohort_period} Activation revenue + {cohort_period} Discounted offer revenue + {cohort_period} Reactivation revenue + {cohort_period} Refund revenue + {cohort_period} Renewal revenue + {cohort_period} Renewal from billing retry revenue + {cohort_period} Unknown revenue</code></td>
      <td><code>subscription_revenue_{cohort_period}</code></td>
    </tr>
    <tr>
      <th colspan="4"><b>Others</b></th>
    </tr>
    <tr>
      <th>N days Subscription ROAS</th>
      <td>Subscription ROAS for the selected cohort period.</td>
      <td><code>{cohort_period} Revenue total / Cost</code></td>
      <td><code>subscription_roas_{cohort_period}</code></td>
    </tr>
  </tbody>
</table>

## [Fraud metrics](fraud-metrics)

These are fraud-related metrics, that help you maintain overview of KPIs such as the number of rejected installs. Read more about [Adjust's fraud solution](/en/article/fraud-prevention-suite). 

<table>
  <thead>
    <tr>
      <th></th>
      <th>Definition</th>
      <th>Formula</th>
      <th>API Metric ID</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <th>Invalid Signature Rejected Install Rate</th>
      <td>Rate of rejected installs due to invalid Signature.</td>
      <td><code>rejected_installs_invalid_signature</code> / (<code>installs</code> + <code>rejected_installs</code>)</td>
      <td>rejected_install_invalid_signature_rate</td>
    </tr>
    <tr>
      <th>Rejected Installs</th>
      <td>The total number of installs that Adjust identified and rejected as fraudulent.</td>
      <td>-</td>
      <td>rejected_installs</td>
    </tr>
    <tr>
      <th>Rejected Install Rate</th>
      <td>The percentage of your total number of installs that Adjust has identified and rejected as fraudulent. The calculation for the Total row excludes installs under Organic and Untrusted Devices.</td>
      <td>(<code>rejected_installs</code> - <code>Organic rejected_installs</code>) / (<code>installs</code> - <code>Organic installs</code> - <code>Untrusted Devices installs</code> + <code>rejected_installs</code> - <code>Organic rejected_installs</code>)</td>
      <td>rejected_install_rate</td>
    </tr>
    <tr>
      <th>Rejected Installs Anonymous IP</th>
      <td>The total number of installs that Adjust rejected because they came from anonymous IPs.</td>
      <td>-</td>
      <td>rejected_installs_anon_ip</td>
    </tr>
    <tr>
      <th>Rejected Installs Anonymous IP Rate</th>
      <td>The percentage of your total number of installs that Adjust has rejected because they came from an anonymous IP.</td>
      <td><code>Rejected installs anon ip</code> / (<code>Installs</code> + <code>Rejected installs</code>)</td>
      <td>rejected_install_anon_ip_rate</td>
    </tr>
    <tr>
      <th>Rejected Installs Click Injection</th>
      <td>The total number of installs Adjust rejected for falsified clicks sent between an app download and install.</td>
      <td>-</td>
      <td>rejected_installs_click_injection</td>
    </tr>
    <tr>
      <th>Rejected Installs Click Injection Rate</th>
      <td>The percentage of your total number of installs that Adjust rejected for falsified clicks sent between an app download and install.</td>
      <td><code>Rejected installs click injection</code> / (<code>Installs</code> + <code>Rejected installs</code>)</td>
      <td>rejected_install_click_injection_rate</td>
    </tr>
    <tr>
      <th>Rejected Installs Distribution Outlier</th>
      <td>The total number of installs Adjust rejected for falling outside the threshold set by our distribution modeling analysis.</td>
      <td>-</td>
      <td>rejected_installs_distribution_outlier</td>
    </tr>
    <tr>
      <th>Rejected Installs Distribution Outlier Rate</th>
      <td>The percentage of your total number of installs that Adjust rejected for falling outside the threshold set by our distribution modeling analysis.</td>
      <td><code>Rejected installs distribution outlier</code> / (<code>Installs</code> + <code>Rejected installs</code>)</td>
      <td>rejected_install_distribution_outlier_rate</td>
    </tr>
    <tr>
      <th>Rejected Installs Malformed Advertising ID</th>
      <td>The total number of installs that Adjust rejected because they had a malformed advertising ID.</td>
      <td>-</td>
      <td>rejected_install_malformed_advertising_id</td>
    </tr>
    <tr>
      <th>Rejected Installs Malformed Advertising ID Rate</th>
      <td>The percentage of your total number of installs that Adjust rejected because of the malformed advertising ID.</td>
      <td>-</td>
      <td>rejected_install_malformed_advertising_id_rate</td>
    </tr>
    <tr>
      <th>Rejected Installs SDK Signature</th>
      <td>The total number of installs Adjust rejected for containing an invalid or missing SDK Signature.</td>
      <td>-</td>
      <td>rejected_installs_invalid_signature</td>
    </tr>
    <tr>
      <th>Rejected Installs Too Many Engagements</th>
      <td>The total number of installs that Adjust rejected for registering too many engagements within the attribution window.</td>
      <td>-</td>
      <td>rejected_installs_too_many_engagements</td>
    </tr>
    <tr>
      <th>Rejected Installs Too Many Engagements Rate</th>
      <td>The percentage of your total number of installs that Adjust rejected for registering too many engagements within the attribution window.</td>
      <td><code>Rejected installs too many engagements</code> / (<code>Installs</code> + <code>Rejected installs</code>)</td>
      <td>rejected_install_too_many_engagements_rate</td>
    </tr>
    <tr>
      <th>Rejected Reattribution</th>
      <td>The total number of reattributions Adjust identified and rejected as fraudulent.</td>
      <td>-</td>
      <td>rejected_reattributions</td>
    </tr>
    <tr>
      <th>Rejected Reattribution Rate</th>
      <td>The percentage of your total number of reattributions that Adjust has identified and rejected as fraudulent. The calculation for the Total row excludes rejected installs under Organic and Untrusted Devices.</td>
      <td><code>Rejected reattributions</code> / (<code>Reattributions</code> + <code>Rejected reattributions</code>)</td>
      <td>rejected_reattribution_rate</td>
    </tr>
    <tr>
      <th>Rejected Reattributions Anonymous IP</th>
      <td>The total number of reattributions that Adjust rejected because they came from an anonymous IP.</td>
      <td>-</td>
      <td>rejected_reattributions_anon_ip</td>
    </tr>
    <tr>
      <th>Rejected Reattributions Anonymous IP Rate</th>
      <td>The percentage of your total number of reattributions that Adjust has rejected because they came from an anonymous IP.</td>
      <td><code>Rejected reattributions anon ip </code>/ (<code>Reattributions</code> + <code>Rejected reattributions</code>)</td>
      <td>rejected_reattribution_anon_ip_rate</td>
    </tr>
    <tr>
      <th>Rejected Reattributions Click Injection</th>
      <td>The total number of reattributions rejected for falsified clicks between an app download and install for a user who previously had your app installed and had that install attributed within Adjust.</td>
      <td>-</td>
      <td>rejected_reattributions_click_injection</td>
    </tr>
    <tr>
      <th>Rejected Reattributions Click Injection Rate</th>
      <td>The percentage of your total number of reattributions that Adjust has rejected for falsified clicks sent between an app download and install for a user who previously had your app installed and had that install attributed within Adjust.</td>
      <td><code>Rejected reattributions click injection</code> / (<code>Reattributions</code> + <code>Rejected reattributions</code>)</td>
      <td>rejected_reattributions_click_injection_rate</td>
    </tr>
    <tr>
      <th>Rejected Reattributions Distribution Outlier</th>
      <td>The total number of reattributions rejected for falling outside the threshold set by our distribution modeling analysis.</td>
      <td>-</td>
      <td>rejected_reattributions_distribution_outlier</td>
    </tr>
    <tr>
      <th>Rejected Reattributions Distribution Outlier Rate</th>
      <td>The percentage of your total number of reattributions that Adjust has rejected for falling outside the threshold set by our distribution modeling analysis.</td>
      <td><code>Rejected reattributions distribution outlier</code> / (<code>Reattributions</code> + <code>Rejected reattributions</code>)</td>
      <td>rejected_reattribution_distribution_outlier_rate</td>
    </tr>
    <tr>
      <th>Rejected Reattributions Too Many Engagements</th>
      <td>The total number of reattributions rejected for registering too many engagements within the reattribution window.</td>
      <td>-</td>
      <td>rejected_reattributions_too_many_engagements</td>
    </tr>
    <tr>
      <th>Rejected Reattributions Too Many Engagements Rate</th>
      <td>The percentage of your total number of reattributions that Adjust has rejected for registering too many engagements within the attribution window.</td>
      <td><code>Rejected reattributions too many engagements</code> / (<code>Reattributions </code>+ <code>Rejected reattributions</code>)</td>
      <td>rejected_reattribution_too_many_engagements_rate</td>
    </tr>
  </tbody>
</table>


## [Assist metrics](assist-metrics)

These Adjust metrics help to measure the role different engagements play in assisting app installs.   

<table>
  <thead>
    <tr>
      <th></th>
      <th>Definition</th>
      <th>Formula</th>
      <th>API Metric ID</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <th>Assisted Installs</th>
      <td>The number of app installs that qualified for attribution but were not selected.&nbsp;Organic users do not have any engagement with an Adjust link, therefore are not considered as assisting an install.</td>
      <td></td>
      <td>assisted_installs</td>
    </tr>
    <tr>
      <th>Assisting Engagements</th>
      <td>All of the user touchpoints that were considered, but not awarded the attribution. These can be clicks or impressions, but must fall within the link's attribution window.</td>
      <td></td>
      <td>qualifiers</td>
    </tr>
    <tr>
      <th>Assisting Impressions</th>
      <td>All of the user impressions that were considered, but not awarded the attribution.&nbsp;</td>
      <td></td>
      <td>impression_based_qualifiers</td>
    </tr>
    <tr>
      <th>Assisting Clicks</th>
      <td>All of the user clicks that were considered, but not awarded the attribution.&nbsp;</td>
      <td></td>
      <td>click_based_qualifiers</td>
    </tr>
    <tr>
      <th>Average Engagements per Assisted Install</th>
      <td>The average number of engagements that assisted an install.&nbsp;</td>
      <td><code>Assisting Engagements / Assisted Installs</code></td>
      <td>qualifiers_per_assisted_installs</td>
    </tr>
    <tr>
      <th>Average Impressions per Assisted Install</th>
      <td>The average number of impressions that assisted an install.</td>
      <td><code>Assisting Impressions / Assisted Installs</code></td>
      <td>impression_based_qualifiers_per_assisted_installs</td>
    </tr>
    <tr>
      <th>Average Clicks per Assisted Install</th>
      <td>The average number of clicks that assisted an install.</td>
      <td><code>Assisting Clicks / Assisted Installs</code></td>
      <td>click_based_qualifiers_per_assisted_installs</td>
    </tr>
    <tr>
      <th>Assisting Clicks for Reattributions</th>
      <td>All of the user clicks that were considered, but not awarded the reattribution.&nbsp;</td>
      <td></td>
      <td>click_based_reattribution_qualifiers</td>
    </tr>
    <tr>
      <th>Assisting Impressions for Reattributions</th>
      <td>All of the user impressions that were considered, but not awarded the reattribution.&nbsp;</td>
      <td></td>
      <td>impression_based_reattribution_qualifiers</td>
    </tr>
    <tr>
      <th>Assisting Engagements for Reattributions</th>
      <td>All of the user touchpoints that were considered, but not awarded the reattribution. These can be clicks or impressions, but must fall within the link's reattribution window.</td>
      <td></td>
      <td>reattribution_qualifiers</td>
    </tr>
    <tr>
      <th>Assisted Reattributions</th>
      <td>The number of app installs that qualified for reattribution but were not selected.&nbsp;</td>
      <td></td>
      <td>assisted_reattributions</td>
    </tr>
    <tr>
      <th>Non-Assisted Installs</th>
      <td>The number of app installs that had no qualifying touchpoints within the attribution window prior to the attributed engagement. Organic users do not have any engagement with an Adjust link, therefore are not considered as assisting an install.</td>
      <td><code>Installs - Assisted installs</code></td>
      <td>non_assisted_installs</td>
    </tr>
  </tbody>
</table>





## [InSight metrics](insight-metrics)

:::{growthSolution}

InSight is available as an Adjust Growth Solution. To get InSight on your account, contact sales@adjust.com.

:::

The following metrics are available for Adjust InSight users and relate to incrementality tests. Find out more about [Adjust InSight](/en/article/insight).

<table>
  <thead>
    <tr>
      <th></th>
      <th>Definition</th>
      <th>Formula</th>
      <th>Metric API ID</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <th>Average revenue per event</th>
      <td>Average revenue generated per your selected event from users who installed your app within the time period you selected</td>
      <td><code>Total revenue of event / number of times the event was triggered</code></td>
      <td><code>average_revenue_per_event</code></td>
    </tr>
    <tr>
      <th>Incremental revenue</th>
      <td>Extra revenue generated from when compared to a control group</td>
      <td><code>(Actual incremental value - mean incremental value) * Average revenue per event</code></td>
      <td><code>incremental_revenue</code></td>
    </tr>
    <tr>
      <th>Incremental ROAS</th>
      <td>Return on advertising spend (ROAS), calculated using only in-app revenue, for a selected cohort period</td>
      <td>-</td>
      <td><code>incremental_roas</code></td>
    </tr>
  </tbody>
</table>

