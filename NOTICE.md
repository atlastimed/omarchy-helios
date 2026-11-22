# Notices

## Gadgetbridge

The strap protocol in `src/helios/` (the Zepp OS handshake, chunked transport,
history fetch and record layouts) was implemented by studying
[Gadgetbridge](https://codeberg.org/Freeyourgadget/Gadgetbridge) at commit
`edbea98cbe1377001c0b8154ac20cb930e1db7f6` (October 2026), which is licensed
under the GNU Affero General Public License v3.0. For that reason `src/helios/`
is released under AGPL-3.0-or-later. Thanks to the Gadgetbridge contributors,
whose reverse engineering made this possible.

The workout summary's fields and the workout type names in
`src/helios/workouts.py` follow Gadgetbridge's `huami.proto` and
`ZeppOsActivityType` at commit `7257742` (November 2026).

## huami-token

`src/helios/zepp.py` fetches the strap's key from your Zepp account with the
requests (addresses, headers and form fields) of
[huami-token](https://codeberg.org/argrento/huami-token) at commit
`1b32658519d1f35cd3c4345bb9ced3ba6881bb56`, which Helios downloaded and ran
for this until 0.7. Thanks to Kirill Snezhko and its contributors. It is
under the MIT License:

> Copyright (c) 2020-2025 Kirill Snezhko
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

## Trademarks and medical use

Helios is not affiliated with, endorsed by or supported by Amazfit or Zepp
Health. "Amazfit", "Helio" and "Zepp" are their trademarks.

Helios is not a medical device. Its readings come from a consumer fitness
band, and are not for diagnosis or treatment.
