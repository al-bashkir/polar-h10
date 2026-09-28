// Command h10 records heart rate, RR intervals and ECG from a Polar H10.
package main

import (
	"os"

	"github.com/al-bashkir/polar-h10/internal/cli"
)

func main() {
	os.Exit(cli.Run(os.Args[1:]))
}
